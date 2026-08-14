import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../application/providers.dart';
import '../../../domain/models.dart';
import '../../widgets/transition_grid.dart';

Color _parseHexColor(String hex) {
  final clean = hex.replaceFirst('#', '');
  return Color(int.parse('FF$clean', radix: 16));
}

const _swatches = [
  '#FFFFFF', '#FFFF00', '#00FFFF', '#FF0000', '#00FF00', '#0000FF', '#FFA500', '#000000',
];

/// One screen, two global choices applied to every exported idea: subtitles
/// on/off (+ style), and transition style.
class Step2Settings extends ConsumerWidget {
  const Step2Settings({super.key, required this.onNext, required this.onBack});
  final VoidCallback onNext;
  final VoidCallback onBack;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final project = ref.watch(currentProjectProvider).project;
    if (project == null) {
      return const Center(child: Text('No project loaded.', style: TextStyle(color: Colors.white54)));
    }

    final subtitlesOn = project.subtitleStyle.enabled && project.export.burnSubtitles;
    final capabilities = ref.watch(capabilitiesProvider);

    void toggleSubtitles(bool on) {
      ref.read(currentProjectProvider.notifier).update((p) => p.copyWith(
            subtitleStyle: p.subtitleStyle.copyWith(enabled: on),
            export: p.export.copyWith(burnSubtitles: on, writeSrt: on),
          ));
    }

    void updateStyle(SubtitleStyle Function(SubtitleStyle) f) {
      ref.read(currentProjectProvider.notifier).update((p) => p.copyWith(subtitleStyle: f(p.subtitleStyle)));
    }

    return Padding(
      padding: const EdgeInsets.all(24),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Expanded(
            child: SingleChildScrollView(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Card(
                    child: SwitchListTile(
                      title: const Text('Subtitles'),
                      subtitle: const Text('Burn subtitles into every exported video'),
                      value: subtitlesOn,
                      onChanged: toggleSubtitles,
                    ),
                  ),
                  if (subtitlesOn) ...[
                    const SizedBox(height: 16),
                    Row(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Expanded(
                          flex: 2,
                          child: _SubtitleStyleControls(
                            style: project.subtitleStyle,
                            capabilities: capabilities,
                            updateStyle: updateStyle,
                          ),
                        ),
                        const SizedBox(width: 24),
                        Expanded(flex: 3, child: _LivePreview(style: project.subtitleStyle)),
                      ],
                    ),
                  ],
                  const SizedBox(height: 24),
                  Text('Transition', style: Theme.of(context).textTheme.titleMedium),
                  const SizedBox(height: 8),
                  capabilities.when(
                    data: (caps) => TransitionGrid(project: project, supportedXfade: caps.xfadeTransitions),
                    loading: () => const Padding(
                      padding: EdgeInsets.symmetric(vertical: 24),
                      child: Center(child: CircularProgressIndicator()),
                    ),
                    error: (e, _) => Text('Could not load capabilities: $e'),
                  ),
                ],
              ),
            ),
          ),
          const SizedBox(height: 16),
          Row(
            mainAxisAlignment: MainAxisAlignment.spaceBetween,
            children: [
              OutlinedButton(onPressed: onBack, child: const Text('Back')),
              FilledButton(onPressed: onNext, child: const Text('Next')),
            ],
          ),
        ],
      ),
    );
  }
}

class _SubtitleStyleControls extends StatelessWidget {
  const _SubtitleStyleControls({required this.style, required this.capabilities, required this.updateStyle});
  final SubtitleStyle style;
  final AsyncValue<Capabilities> capabilities;
  final void Function(SubtitleStyle Function(SubtitleStyle)) updateStyle;

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        capabilities.when(
          data: (caps) => DropdownButtonFormField<String>(
            initialValue: caps.fonts.contains(style.fontFamily) ? style.fontFamily : null,
            decoration: const InputDecoration(labelText: 'Font'),
            items: [for (final f in caps.fonts) DropdownMenuItem(value: f, child: Text(f))],
            onChanged: (v) => v == null ? null : updateStyle((s) => s.copyWith(fontFamily: v)),
          ),
          loading: () => const LinearProgressIndicator(),
          error: (_, _) => const Text('Could not load font list'),
        ),
        const SizedBox(height: 12),
        Row(
          children: [
            const Text('Size'),
            Expanded(
              child: Slider(
                value: style.fontSize.toDouble(),
                min: 16,
                max: 96,
                divisions: 80,
                label: '${style.fontSize}',
                onChanged: (v) => updateStyle((s) => s.copyWith(fontSize: v.round())),
              ),
            ),
          ],
        ),
        Row(
          children: [
            const Text('Outline width'),
            Expanded(
              child: Slider(
                value: style.outlineWidth,
                min: 0,
                max: 8,
                divisions: 16,
                label: style.outlineWidth.toStringAsFixed(1),
                onChanged: (v) => updateStyle((s) => s.copyWith(outlineWidth: v)),
              ),
            ),
          ],
        ),
        Row(
          children: [
            const Text('Shadow'),
            Expanded(
              child: Slider(
                value: style.shadow,
                min: 0,
                max: 8,
                divisions: 16,
                label: style.shadow.toStringAsFixed(1),
                onChanged: (v) => updateStyle((s) => s.copyWith(shadow: v)),
              ),
            ),
          ],
        ),
        const SizedBox(height: 8),
        const Text('Text color'),
        _ColorSwatchRow(selected: style.primaryColor, onSelect: (hex) => updateStyle((s) => s.copyWith(primaryColor: hex))),
        const SizedBox(height: 8),
        const Text('Outline color'),
        _ColorSwatchRow(selected: style.outlineColor, onSelect: (hex) => updateStyle((s) => s.copyWith(outlineColor: hex))),
        const SizedBox(height: 12),
        DropdownButtonFormField<String>(
          initialValue: style.position,
          decoration: const InputDecoration(labelText: 'Position'),
          items: const [
            DropdownMenuItem(value: 'bottom', child: Text('Bottom')),
            DropdownMenuItem(value: 'center', child: Text('Center')),
            DropdownMenuItem(value: 'top', child: Text('Top')),
          ],
          onChanged: (v) => v == null ? null : updateStyle((s) => s.copyWith(position: v)),
        ),
      ],
    );
  }
}

class _ColorSwatchRow extends StatelessWidget {
  const _ColorSwatchRow({required this.selected, required this.onSelect});
  final String selected;
  final void Function(String hex) onSelect;

  @override
  Widget build(BuildContext context) {
    return Wrap(
      spacing: 8,
      children: [
        for (final hex in _swatches)
          InkWell(
            onTap: () => onSelect(hex),
            child: Container(
              width: 28,
              height: 28,
              decoration: BoxDecoration(
                color: _parseHexColor(hex),
                shape: BoxShape.circle,
                border: Border.all(
                  color: selected.toUpperCase() == hex ? Colors.white : Colors.white24,
                  width: selected.toUpperCase() == hex ? 2 : 1,
                ),
              ),
            ),
          ),
      ],
    );
  }
}

class _LivePreview extends StatelessWidget {
  const _LivePreview({required this.style});
  final SubtitleStyle style;

  Alignment get _alignment => switch (style.position) {
        'top' => Alignment.topCenter,
        'center' => Alignment.center,
        _ => Alignment.bottomCenter,
      };

  @override
  Widget build(BuildContext context) {
    return AspectRatio(
      aspectRatio: 16 / 9,
      child: Container(
        decoration: BoxDecoration(color: Colors.black, borderRadius: BorderRadius.circular(8)),
        child: Align(
          alignment: _alignment,
          child: Padding(
            padding: EdgeInsets.only(bottom: style.position == 'bottom' ? style.marginV.toDouble() : 0),
            child: Text(
              'Сайн байна уу — sample subtitle',
              style: TextStyle(
                fontFamily: style.fontFamily,
                fontSize: style.fontSize / 2, // preview box is smaller than the 1080p render canvas
                color: _parseHexColor(style.primaryColor),
                fontWeight: FontWeight.bold,
                shadows: [
                  for (var i = 0; i < style.outlineWidth.round().clamp(0, 6); i++)
                    Shadow(color: _parseHexColor(style.outlineColor), blurRadius: 1, offset: Offset.zero),
                  if (style.shadow > 0)
                    Shadow(
                      color: Colors.black.withValues(alpha: 0.8),
                      blurRadius: style.shadow,
                      offset: Offset(style.shadow / 2, style.shadow / 2),
                    ),
                ],
              ),
            ),
          ),
        ),
      ),
    );
  }
}
