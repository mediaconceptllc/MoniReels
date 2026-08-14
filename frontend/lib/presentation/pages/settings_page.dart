import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../application/providers.dart';
import '../../core/errors.dart';
import '../../core/result.dart';
import '../../domain/models.dart';
import '../widgets/error_view.dart';

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
                  const SizedBox(height: 4),
                  const Text(
                    'Saved to the backend\'s .env file and applied immediately — no restart needed.',
                    style: TextStyle(color: Colors.white54, fontSize: 12),
                  ),
                  const SizedBox(height: 12),
                  const _CredentialsSection(),
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

class _CredentialsSection extends ConsumerWidget {
  const _CredentialsSection();

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final settingsAsync = ref.watch(backendSettingsProvider);

    return settingsAsync.when(
      data: (settings) => _CredentialsForm(settings: settings),
      loading: () => const LinearProgressIndicator(),
      error: (e, _) => ErrorView(error: e is AppError ? e : UnknownError(e.toString())),
    );
  }
}

class _CredentialsForm extends ConsumerStatefulWidget {
  const _CredentialsForm({required this.settings});
  final BackendSettings settings;

  @override
  ConsumerState<_CredentialsForm> createState() => _CredentialsFormState();
}

class _CredentialsFormState extends ConsumerState<_CredentialsForm> {
  late final _chimegeTokenController = TextEditingController();
  late final _openaiKeyController = TextEditingController();
  late final _openaiModelController = TextEditingController(text: widget.settings.openaiModel);
  late final _chimegeUrlController = TextEditingController(text: widget.settings.chimegeSttUrl);
  late final _openaiBaseUrlController = TextEditingController(text: widget.settings.openaiBaseUrl);
  late final _chimegeMaxAudioController =
      TextEditingController(text: widget.settings.chimegeMaxAudioSec.toString());

  bool _expandedAdvanced = false;

  @override
  void dispose() {
    _chimegeTokenController.dispose();
    _openaiKeyController.dispose();
    _openaiModelController.dispose();
    _chimegeUrlController.dispose();
    _openaiBaseUrlController.dispose();
    _chimegeMaxAudioController.dispose();
    super.dispose();
  }

  void _showMessage(String text) {
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(text), duration: const Duration(seconds: 2)));
  }

  Future<void> _saveField(Future<Result<BackendSettings>> Function() call, {VoidCallback? onSaved}) async {
    final result = await call();
    result.when(
      ok: (_) {
        onSaved?.call();
        ref.read(settingsGenerationProvider.notifier).state++;
        _showMessage('Saved.');
      },
      err: (e) => _showMessage('Save failed: ${e.message}'),
    );
  }

  @override
  Widget build(BuildContext context) {
    final repo = ref.read(repositoryProvider);
    final settings = widget.settings;

    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        _TokenField(
          label: 'Chimege token',
          isSet: settings.chimegeTokenSet,
          controller: _chimegeTokenController,
          onSave: () => _saveField(
            () => repo.updateSettings(chimegeToken: _chimegeTokenController.text.trim()),
            onSaved: _chimegeTokenController.clear,
          ),
        ),
        const SizedBox(height: 12),
        _TokenField(
          label: 'OpenAI API key',
          isSet: settings.openaiApiKeySet,
          controller: _openaiKeyController,
          onSave: () => _saveField(
            () => repo.updateSettings(openaiApiKey: _openaiKeyController.text.trim()),
            onSaved: _openaiKeyController.clear,
          ),
        ),
        const SizedBox(height: 12),
        _PlainField(
          label: 'OpenAI model',
          hintText: 'e.g. gpt-4.1',
          controller: _openaiModelController,
          onSave: () => _saveField(() => repo.updateSettings(openaiModel: _openaiModelController.text.trim())),
        ),
        const SizedBox(height: 8),
        _CollapsibleSection(
          title: 'Advanced',
          expanded: _expandedAdvanced,
          onToggle: () => setState(() => _expandedAdvanced = !_expandedAdvanced),
          children: [
            _PlainField(
              label: 'Chimege STT base URL',
              controller: _chimegeUrlController,
              onSave: () =>
                  _saveField(() => repo.updateSettings(chimegeSttUrl: _chimegeUrlController.text.trim())),
            ),
            const SizedBox(height: 12),
            _PlainField(
              label: 'OpenAI base URL',
              controller: _openaiBaseUrlController,
              onSave: () =>
                  _saveField(() => repo.updateSettings(openaiBaseUrl: _openaiBaseUrlController.text.trim())),
            ),
            const SizedBox(height: 12),
            _PlainField(
              label: 'Chimege max sync-transcribe audio (seconds)',
              controller: _chimegeMaxAudioController,
              keyboardType: TextInputType.number,
              onSave: () {
                final parsed = int.tryParse(_chimegeMaxAudioController.text.trim());
                if (parsed == null) {
                  _showMessage('Enter a whole number of seconds.');
                  return;
                }
                _saveField(() => repo.updateSettings(chimegeMaxAudioSec: parsed));
              },
            ),
          ],
        ),
      ],
    );
  }
}

class _TokenField extends StatefulWidget {
  const _TokenField({
    required this.label,
    required this.isSet,
    required this.controller,
    required this.onSave,
  });
  final String label;
  final bool isSet;
  final TextEditingController controller;
  final VoidCallback onSave;

  @override
  State<_TokenField> createState() => _TokenFieldState();
}

class _TokenFieldState extends State<_TokenField> {
  bool _obscure = true;

  @override
  Widget build(BuildContext context) {
    return Row(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        // A hidden field left empty after saving still *looks* empty at a
        // glance (hintText is faint and disappears once you start typing),
        // which reads as "did my token even save?" - this status line is
        // separate from the field itself and never depends on its content,
        // so it stays visible and unambiguous regardless of what's typed.
        Expanded(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Row(
                children: [
                  Icon(
                    widget.isSet ? Icons.check_circle : Icons.radio_button_unchecked,
                    size: 14,
                    color: widget.isSet ? Colors.greenAccent : Colors.white38,
                  ),
                  const SizedBox(width: 6),
                  Text(
                    widget.isSet ? '${widget.label}: key saved' : '${widget.label}: not set',
                    style: TextStyle(
                      fontSize: 12,
                      color: widget.isSet ? Colors.greenAccent : Colors.white54,
                      fontWeight: FontWeight.w500,
                    ),
                  ),
                ],
              ),
              const SizedBox(height: 4),
              TextField(
                controller: widget.controller,
                obscureText: _obscure,
                decoration: InputDecoration(
                  labelText: widget.label,
                  hintText: widget.isSet ? 'Enter a new value to replace it' : 'Paste key/token here',
                  suffixIcon: IconButton(
                    icon: Icon(_obscure ? Icons.visibility_off : Icons.visibility, size: 18),
                    onPressed: () => setState(() => _obscure = !_obscure),
                  ),
                ),
              ),
            ],
          ),
        ),
        const SizedBox(width: 12),
        Padding(
          padding: const EdgeInsets.only(top: 26),
          child: FilledButton(onPressed: widget.onSave, child: const Text('Save')),
        ),
      ],
    );
  }
}

/// Deliberately not ExpansionTile: expanding/collapsing it fires a
/// SemanticsService accessibility announcement that crashes on Windows in
/// Flutter 3.38+ ("Announce message 'viewId' property must be a
/// FlutterViewId", https://github.com/flutter/flutter/issues/179563).
/// This does the same collapsible-section job without going anywhere near
/// that code path.
class _CollapsibleSection extends StatelessWidget {
  const _CollapsibleSection({
    required this.title,
    required this.expanded,
    required this.onToggle,
    required this.children,
  });
  final String title;
  final bool expanded;
  final VoidCallback onToggle;
  final List<Widget> children;

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        InkWell(
          onTap: onToggle,
          child: Padding(
            padding: const EdgeInsets.symmetric(vertical: 8),
            child: Row(
              children: [
                Icon(expanded ? Icons.expand_less : Icons.expand_more, size: 20),
                const SizedBox(width: 4),
                Text(title, style: Theme.of(context).textTheme.titleSmall),
              ],
            ),
          ),
        ),
        AnimatedCrossFade(
          firstChild: const SizedBox(width: double.infinity),
          secondChild: Padding(
            padding: const EdgeInsets.only(top: 4),
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: children),
          ),
          crossFadeState: expanded ? CrossFadeState.showSecond : CrossFadeState.showFirst,
          duration: const Duration(milliseconds: 150),
        ),
      ],
    );
  }
}

class _PlainField extends StatelessWidget {
  const _PlainField({
    required this.label,
    required this.controller,
    required this.onSave,
    this.hintText,
    this.keyboardType,
  });

  final String label;
  final TextEditingController controller;
  final VoidCallback onSave;
  final String? hintText;
  final TextInputType? keyboardType;

  @override
  Widget build(BuildContext context) {
    return Row(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Expanded(
          child: TextField(
            controller: controller,
            keyboardType: keyboardType,
            decoration: InputDecoration(labelText: label, hintText: hintText),
          ),
        ),
        const SizedBox(width: 12),
        FilledButton(onPressed: onSave, child: const Text('Save')),
      ],
    );
  }
}
