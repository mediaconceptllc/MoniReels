import 'dart:ui' show AppExitResponse;

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../application/backend_launcher.dart';
import '../application/providers.dart';
import '../core/errors.dart';
import 'pages/ffmpeg_setup_page.dart';
import 'shell.dart';
import 'widgets/error_view.dart';

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
  AppLifecycleListener? _lifecycleListener;

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
    _lifecycleListener?.dispose();
    _launcher.killIfSpawned();
    super.dispose();
  }

  Future<void> _launch() async {
    setState(() {
      _launchError = null;
      _status = 'Connecting to backend...';
    });
    try {
      final devUrl = Uri.parse(ref.read(apiClientProvider).baseUrl);
      final resolvedUrl = await _launcher.ensureRunning(devUrl: devUrl);
      ref.read(apiClientProvider).updateBaseUrl(resolvedUrl.toString());
      ref.read(backendGenerationProvider.notifier).state++;
    } catch (e) {
      setState(() => _launchError = _asDisplayError(e));
    }
  }

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

    final health = ref.watch(healthProvider);
    return health.when(
      data: (data) => data['ffmpeg'] == true ? const AppShell() : const FfmpegSetupPage(),
      loading: () => Scaffold(
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
      ),
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
