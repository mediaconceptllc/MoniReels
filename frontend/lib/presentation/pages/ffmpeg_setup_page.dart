import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../application/providers.dart';

const _ffmpegDownloadUrl = 'https://www.gyan.dev/ffmpeg/builds/';

/// Shown instead of the main app shell when the backend reports
/// `ffmpeg: false` from /health — per spec §8.1, the app never crashes on
/// a missing FFmpeg, it guides the user to install one.
class FfmpegSetupPage extends ConsumerWidget {
  const FfmpegSetupPage({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    return Scaffold(
      body: Center(
        child: ConstrainedBox(
          constraints: const BoxConstraints(maxWidth: 480),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              const Icon(Icons.warning_amber_rounded, size: 56, color: Colors.orangeAccent),
              const SizedBox(height: 16),
              Text('FFmpeg not found', style: Theme.of(context).textTheme.headlineSmall),
              const SizedBox(height: 12),
              const Text(
                'This app needs FFmpeg and FFprobe to import, transcribe, and export video. '
                'Download a build, then either add it to your PATH, place ffmpeg.exe / ffprobe.exe '
                'in the backend\'s bin/ folder, or set FFMPEG_PATH in the backend\'s .env file.',
                textAlign: TextAlign.center,
              ),
              const SizedBox(height: 24),
              FilledButton.icon(
                onPressed: () => Process.run('explorer', [_ffmpegDownloadUrl]),
                icon: const Icon(Icons.download),
                label: const Text('Download FFmpeg'),
              ),
              const SizedBox(height: 12),
              OutlinedButton.icon(
                onPressed: () {
                  ref.invalidate(healthProvider);
                  ref.invalidate(capabilitiesProvider);
                },
                icon: const Icon(Icons.refresh),
                label: const Text('Check again'),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
