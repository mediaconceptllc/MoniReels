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
    if (await _isHealthy(devUrl)) {
      return devUrl;
    }

    final backendExe = _locateBundledExe();
    if (backendExe == null || !backendExe.existsSync()) {
      throw StateError(
        'Backend is not running at $devUrl and no bundled backend executable '
        'was found. Start the backend manually or check your installation.',
      );
    }

    final process = await Process.start(
      backendExe.path,
      const [],
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
    unawaited(process.exitCode.then((code) {
      if (!readyCompleter.isCompleted) {
        readyCompleter.completeError(StateError('Backend process exited early (code $code) before signaling READY.'));
      }
    }));

    final port = await readyCompleter.future.timeout(
      const Duration(seconds: 20),
      onTimeout: () => throw StateError('Backend did not signal READY within 20s.'),
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

  Future<bool> _isHealthy(Uri url) async {
    final client = HttpClient()..connectionTimeout = const Duration(seconds: 1);
    try {
      final request = await client.getUrl(url.replace(path: '/health')).timeout(const Duration(seconds: 1));
      final response = await request.close().timeout(const Duration(seconds: 1));
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
