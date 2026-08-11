import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../application/providers.dart';

class SettingsPage extends ConsumerStatefulWidget {
  const SettingsPage({super.key});

  @override
  ConsumerState<SettingsPage> createState() => _SettingsPageState();
}

class _SettingsPageState extends ConsumerState<SettingsPage> {
  late final TextEditingController _urlController =
      TextEditingController(text: ref.read(apiClientProvider).baseUrl);

  @override
  void dispose() {
    _urlController.dispose();
    super.dispose();
  }

  void _applyUrl() {
    ref.read(apiClientProvider).updateBaseUrl(_urlController.text.trim());
    ref.read(backendGenerationProvider.notifier).state++;
  }

  @override
  Widget build(BuildContext context) {
    final health = ref.watch(healthProvider);
    final capabilities = ref.watch(capabilitiesProvider);

    return Padding(
      padding: const EdgeInsets.all(24),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text('Settings', style: Theme.of(context).textTheme.headlineSmall),
          const SizedBox(height: 16),
          Expanded(
            child: SingleChildScrollView(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text('Backend', style: Theme.of(context).textTheme.titleMedium),
                  const SizedBox(height: 8),
                  Row(
                    children: [
                      Expanded(
                        child: TextField(
                          controller: _urlController,
                          decoration: const InputDecoration(labelText: 'Backend URL'),
                          onSubmitted: (_) => _applyUrl(),
                        ),
                      ),
                      const SizedBox(width: 12),
                      FilledButton(onPressed: _applyUrl, child: const Text('Apply')),
                    ],
                  ),
                  const SizedBox(height: 8),
                  health.when(
                    data: (h) => Text(
                      h['ffmpeg'] == true
                          ? 'Connected. FFmpeg ${h['version']}'
                          : 'Connected, but FFmpeg was not found by the backend.',
                      style: TextStyle(color: h['ffmpeg'] == true ? Colors.greenAccent : Colors.orangeAccent),
                    ),
                    loading: () => const Text('Checking...'),
                    error: (e, _) => const Text('Could not reach backend', style: TextStyle(color: Colors.redAccent)),
                  ),
                  const Divider(height: 32),
                  Text('FFmpeg capabilities', style: Theme.of(context).textTheme.titleMedium),
                  const SizedBox(height: 8),
                  capabilities.when(
                    data: (caps) => Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text('${caps.xfadeTransitions.length} transitions supported'),
                        Text('${caps.encodersListed.length} encoders listed'),
                        Text('Hardware encoder: ${caps.workingHwaccelEncoder ?? "none (using libx264)"}'),
                        Text('${caps.fonts.length} fonts available'),
                      ],
                    ),
                    loading: () => const Text('Loading...'),
                    error: (e, _) => const Text('Unavailable', style: TextStyle(color: Colors.white54)),
                  ),
                  const Divider(height: 32),
                  Text('Credentials', style: Theme.of(context).textTheme.titleMedium),
                  const SizedBox(height: 8),
                  const Text(
                    'Chimege STT and OpenAI credentials are configured via the backend\'s '
                    '.env file (CHIMEGE_STT_URL, CHIMEGE_TOKEN, OPENAI_API_KEY, OPENAI_MODEL) — '
                    'never entered here, so secrets never live in this app\'s state or logs.',
                    style: TextStyle(color: Colors.white54),
                  ),
                  const Divider(height: 32),
                  Text('Logs', style: Theme.of(context).textTheme.titleMedium),
                  const SizedBox(height: 8),
                  FilledButton.icon(
                    onPressed: _openLogsFolder,
                    icon: const Icon(Icons.folder_open),
                    label: const Text('Open logs folder'),
                  ),
                ],
              ),
            ),
          ),
        ],
      ),
    );
  }

  void _openLogsFolder() {
    final appData = Platform.environment['APPDATA'];
    if (appData == null) return;
    final logsDir = '$appData\\AIVideoEditor\\logs';
    Process.run('explorer', [logsDir]);
  }
}
