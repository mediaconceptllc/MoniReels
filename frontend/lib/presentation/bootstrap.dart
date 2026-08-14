import 'dart:async';
import 'dart:ui' show AppExitResponse;

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../application/backend_launcher.dart';
import '../application/providers.dart';
import '../core/errors.dart';
import 'pages/ffmpeg_setup_page.dart';
import 'pages/wizard/wizard_shell.dart';
import 'widgets/error_view.dart';

/// Backend self-terminates (app/main.py's idle-shutdown watcher) after
/// idle_shutdown_sec (60s default) with no HTTP request — a safety net for
/// orphaned processes, not meant to police normal usage pace. Without a
/// heartbeat, any 60+s gap between user actions (reading a transcript,
/// thinking, this app just sitting open) kills the backend, surfacing as
/// "could not reach backend" on the next click. Ping well under that
/// interval so the backend only ever goes idle if the app itself is gone.
const _kHeartbeatInterval = Duration(seconds: 20);

/// healthProvider/capabilitiesProvider re-throw the AppError a repository
/// call already produced — display it as-is instead of stringifying and
/// re-wrapping it (which used to print "Instance of 'NetworkError'").
AppError _asDisplayError(Object e) => e is AppError ? e : UnknownError(e.toString());

/// Launches (or reuses) the backend, then routes to either the FFmpeg setup
/// screen or the main app shell depending on /health, per spec §8.1/§10.
class AppBootstrap extends ConsumerStatefulWidget {
  const AppBootstrap({super.key});

  @override
  ConsumerState<AppBootstrap> createState() => _AppBootstrapState();
}

class _AppBootstrapState extends ConsumerState<AppBootstrap> {
  late final BackendLauncher _launcher = ref.read(backendLauncherProvider);
  String _status = 'Connecting to backend...';
  AppError? _launchError;
  // True for the whole span of _launch() - covers BackendLauncher.
  // ensureRunning() finding/spawning the process *and* waiting for its
  // READY signal, which can genuinely take a while on first run (up to its
  // own internal timeouts). While this is true, healthProvider is never
  // watched, so its very first request - fired the instant this widget
  // builds, against a backend that may not be listening yet - can't flash
  // an error screen before _launch() has even had a chance to finish.
  bool _launching = true;
  AppLifecycleListener? _lifecycleListener;
  Timer? _heartbeatTimer;

  @override
  void initState() {
    super.initState();
    _lifecycleListener = AppLifecycleListener(
      onExitRequested: () async {
        _launcher.killIfSpawned();
        return AppExitResponse.exit;
      },
    );
    _launch();
  }

  @override
  void dispose() {
    _heartbeatTimer?.cancel();
    _lifecycleListener?.dispose();
    _launcher.killIfSpawned();
    super.dispose();
  }

  Future<void> _launch() async {
    setState(() {
      _launchError = null;
      _status = 'Connecting to backend...';
      _launching = true;
    });
    try {
      final devUrl = Uri.parse(ref.read(apiClientProvider).baseUrl);
      final resolvedUrl = await _launcher.ensureRunning(devUrl: devUrl);
      ref.read(apiClientProvider).updateBaseUrl(resolvedUrl.toString());
      ref.read(backendGenerationProvider.notifier).state++;
      _startHeartbeat();
      setState(() => _launching = false);
    } catch (e) {
      setState(() {
        _launchError = _asDisplayError(e);
        _launching = false;
      });
    }
  }

  void _startHeartbeat() {
    _heartbeatTimer?.cancel();
    _heartbeatTimer = Timer.periodic(_kHeartbeatInterval, (_) {
      // Fire-and-forget: any request at all resets the backend's
      // _last_request_time, so a failure here doesn't need handling — the
      // next real user action (or the next tick) will surface/retry it.
      unawaited(ref.read(repositoryProvider).health());
    });
  }

  Widget _loadingScaffold() => Scaffold(
        body: Center(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              const CircularProgressIndicator(),
              const SizedBox(height: 16),
              Text(_status, style: const TextStyle(color: Colors.white54)),
            ],
          ),
        ),
      );

  @override
  Widget build(BuildContext context) {
    if (_launchError != null) {
      return Scaffold(
        body: Center(
          child: ConstrainedBox(
            constraints: const BoxConstraints(maxWidth: 480),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                ErrorView(error: _launchError!),
                const SizedBox(height: 16),
                FilledButton(onPressed: _launch, child: const Text('Retry')),
              ],
            ),
          ),
        ),
      );
    }

    // _launch() (BackendLauncher.ensureRunning) already waits for the
    // backend's READY signal and a successful /health poll before ever
    // returning - so healthProvider is only ever watched *after* that's
    // confirmed. Watching it earlier raced its first, eager request
    // against a backend that might not be listening yet, flashing an
    // error screen that then "fixed itself" once the launch caught up.
    if (_launching) {
      return _loadingScaffold();
    }

    final health = ref.watch(healthProvider);
    return health.when(
      data: (data) => data['ffmpeg'] == true ? const WizardPage() : const FfmpegSetupPage(),
      loading: _loadingScaffold,
      error: (e, _) => Scaffold(
        body: Center(
          child: ConstrainedBox(
            constraints: const BoxConstraints(maxWidth: 480),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                ErrorView(error: _asDisplayError(e)),
                const SizedBox(height: 16),
                FilledButton(onPressed: () => ref.invalidate(healthProvider), child: const Text('Retry')),
              ],
            ),
          ),
        ),
      ),
    );
  }
}
