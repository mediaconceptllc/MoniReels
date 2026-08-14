import 'dart:async';
import 'dart:convert';
import 'dart:io';

/// Finds or starts the backend, per spec §10:
/// - If a backend is already reachable at the dev URL, reuse it (dev mode).
/// - Otherwise spawn the PyInstaller-built exe bundled next to this Flutter
///   exe, read a "READY " line (followed by the port number) from its
///   stdout, and use that port.
///
/// Kill-on-crash is handled by the backend itself (its own idle-shutdown
/// heartbeat in --managed mode, see app/main.py) rather than a Windows Job
/// Object here — the spec explicitly allows either approach, and the
/// heartbeat needs no native interop from the Flutter side.
class BackendLauncher {
  Process? _process;

  Future<Uri> ensureRunning({required Uri devUrl}) async {
    // Retried, not a single shot: a backend that's actually alive and fine
    // can still miss one 1-2s health check if the system is briefly busy
    // (e.g. right after encoding several videos back to back) - treating
    // that single miss as "the process is dead" triggered an unnecessary
    // kill-and-respawn of a perfectly healthy backend, which is worse than
    // the transient slowness it was reacting to.
    if (await _isHealthyWithRetries(devUrl)) {
      return devUrl;
    }

    final backendExe = _locateBundledExe();
    if (backendExe != null && backendExe.existsSync()) {
      return _spawnAndWaitForReady(
        executable: backendExe.path,
        arguments: const [],
        workingDirectory: backendExe.parent.path,
      );
    }

    // Dev mode: no packaged exe next to this Flutter binary - spawn the
    // Python backend straight from source instead of requiring it to
    // already be running. `flutter run` is invoked from `frontend/`, so
    // `backend/` is a sibling of the current working directory.
    final devBackendDir = _locateDevBackendDir();
    final devPython = devBackendDir == null
        ? null
        : File('${devBackendDir.path}${Platform.pathSeparator}.venv${Platform.pathSeparator}Scripts${Platform.pathSeparator}python.exe');
    if (devBackendDir != null && devPython != null && devPython.existsSync()) {
      return _spawnAndWaitForReady(
        executable: devPython.path,
        arguments: const ['-m', 'app.main'],
        workingDirectory: devBackendDir.path,
      );
    }

    throw StateError(
      'Backend is not running at $devUrl, and neither a packaged backend '
      'executable nor a dev backend (../backend with a .venv) was found. '
      'Start the backend manually or check your installation.',
    );
  }

  Future<Uri> _spawnAndWaitForReady({
    required String executable,
    required List<String> arguments,
    required String workingDirectory,
  }) async {
    // Defense in depth against ever running two backends at once: if this
    // launcher already has one tracked (e.g. a reconnect got triggered
    // while the previous spawn was still alive - two duplicate export jobs
    // once fought over the same output files this way), kill it first
    // rather than leaking it and adding a second one on top.
    killIfSpawned();

    final process = await Process.start(
      executable,
      arguments,
      // Without this, the child inherits *our* cwd, not its own folder — and
      // Settings.model_config's env_file=".env" (backend/app/config.py) is
      // resolved relative to cwd, so a wrong cwd means .env silently fails
      // to load.
      workingDirectory: workingDirectory,
      // MANAGED=true turns on the backend's own idle-shutdown heartbeat
      // (app/main.py's _idle_shutdown_watcher) — a safety net so the
      // backend still exits on its own if the Flutter process is killed
      // hard enough that killIfSpawned() never runs.
      environment: {'MANAGED': 'true'},
      includeParentEnvironment: true,
      mode: ProcessStartMode.normal,
    );
    _process = process;

    final readyCompleter = Completer<int>();
    process.stdout.transform(utf8.decoder).transform(const LineSplitter()).listen((line) {
      final match = RegExp(r'^READY (\d+)$').firstMatch(line);
      if (match != null && !readyCompleter.isCompleted) {
        readyCompleter.complete(int.parse(match.group(1)!));
      }
    });
    // Must be drained even though we don't parse it: an unread stderr pipe
    // fills up once the backend writes enough to it (Python warnings,
    // logging's own error-fallback path, ...) and every further write from
    // the child then blocks on the full pipe - which freezes its whole
    // single-threaded event loop, taking every in-flight request down with
    // it. Printing it through stderr here keeps it visible for debugging.
    process.stderr.transform(utf8.decoder).transform(const LineSplitter()).listen((line) {
      stderr.writeln('[backend] $line');
    });
    unawaited(process.exitCode.then((code) {
      if (!readyCompleter.isCompleted) {
        readyCompleter.completeError(StateError('Backend process exited early (code $code) before signaling READY.'));
      }
    }));

    // A minute, not the previous 20s: a freshly-extracted/first-run backend
    // exe can genuinely take a while to start (e.g. antivirus scanning it,
    // a cold disk cache) - a tighter timeout was firing as a false "backend
    // error" on slow-but-otherwise-fine launches.
    final port = await readyCompleter.future.timeout(
      const Duration(minutes: 1),
      onTimeout: () => throw StateError('Backend did not signal READY within 60s.'),
    );
    final url = Uri.parse('http://127.0.0.1:$port');

    // Defense in depth: the backend only prints READY once its listen socket
    // is bound (see app/main.py), but retry briefly anyway rather than trust
    // a single request on a freshly spawned process.
    for (var attempt = 0; attempt < 10; attempt++) {
      if (await _isHealthy(url)) return url;
      await Future<void>.delayed(const Duration(milliseconds: 200));
    }
    throw StateError('Backend signaled READY on port $port but never responded to /health.');
  }

  File? _locateBundledExe() {
    // Packaged layout: `install_dir/ai_video_editor_frontend.exe` next to
    // `install_dir/backend/ai_video_editor_backend.exe`.
    final exeDir = File(Platform.resolvedExecutable).parent;
    final candidate = File('${exeDir.path}${Platform.pathSeparator}backend${Platform.pathSeparator}ai_video_editor_backend.exe');
    return candidate;
  }

  Directory? _locateDevBackendDir() {
    final candidate = Directory('${Directory.current.path}${Platform.pathSeparator}..${Platform.pathSeparator}backend');
    return candidate.existsSync() ? candidate : null;
  }

  Future<bool> _isHealthyWithRetries(Uri url, {int attempts = 3}) async {
    for (var attempt = 0; attempt < attempts; attempt++) {
      if (await _isHealthy(url)) return true;
      if (attempt < attempts - 1) {
        await Future<void>.delayed(const Duration(milliseconds: 400));
      }
    }
    return false;
  }

  Future<bool> _isHealthy(Uri url) async {
    // 2s, not the previous 1s: same reasoning as the 60s startup timeout
    // above - generous enough that a briefly-busy-but-alive backend isn't
    // mistaken for a dead one.
    final client = HttpClient()..connectionTimeout = const Duration(seconds: 2);
    try {
      final request = await client.getUrl(url.replace(path: '/health')).timeout(const Duration(seconds: 2));
      final response = await request.close().timeout(const Duration(seconds: 2));
      await response.drain<void>();
      return response.statusCode == 200;
    } catch (_) {
      return false;
    } finally {
      client.close(force: true);
    }
  }

  /// Only kills a backend *this launcher spawned* — a reused dev-mode backend
  /// is left running, since it wasn't ours to manage.
  void killIfSpawned() {
    _process?.kill();
    _process = null;
  }
}
