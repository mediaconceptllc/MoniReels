import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../application/providers.dart';
import '../../domain/models.dart';
import '../../domain/transition_registry.dart';

/// Transition-type picker + duration slider, writing to the single
/// project-wide `project.transition` — applied to every exported idea in
/// the wizard's Step 3 (only a youtube idea's multiple keep-ranges actually
/// need a transition between them; a single-clip reel render ignores it).
class TransitionGrid extends ConsumerWidget {
  const TransitionGrid({super.key, required this.project, required this.supportedXfade});
  final Project project;
  final List<String> supportedXfade;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final current = project.transition;

    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        GridView.builder(
          shrinkWrap: true,
          physics: const NeverScrollableScrollPhysics(),
          gridDelegate: const SliverGridDelegateWithMaxCrossAxisExtent(
            maxCrossAxisExtent: 180,
            mainAxisExtent: 90,
            crossAxisSpacing: 12,
            mainAxisSpacing: 12,
          ),
          itemCount: kTransitionRegistry.length,
          itemBuilder: (context, i) {
            final t = kTransitionRegistry[i];
            final supported = isTransitionSupported(t, supportedXfade);
            final selected = current.type == t.uiName;
            return Tooltip(
              message: supported ? t.uiName : '${t.uiName} — not supported by this FFmpeg build',
              child: Card(
                color: selected ? Theme.of(context).colorScheme.primaryContainer : null,
                child: InkWell(
                  onTap: supported
                      ? () => ref
                          .read(currentProjectProvider.notifier)
                          .update((p) => p.copyWith(transition: current.copyWith(type: t.uiName)))
                      : null,
                  child: Opacity(
                    opacity: supported ? 1.0 : 0.35,
                    child: Center(
                      child: Text(t.uiName, textAlign: TextAlign.center),
                    ),
                  ),
                ),
              ),
            );
          },
        ),
        const SizedBox(height: 16),
        Row(
          children: [
            const Text('Duration'),
            Expanded(
              child: Slider(
                value: current.duration.clamp(kMinTransitionDuration, kMaxTransitionDuration),
                min: kMinTransitionDuration,
                max: kMaxTransitionDuration,
                divisions: ((kMaxTransitionDuration - kMinTransitionDuration) / kTransitionDurationStep).round(),
                label: '${current.duration.toStringAsFixed(2)}s',
                onChanged: (v) => ref
                    .read(currentProjectProvider.notifier)
                    .update((p) => p.copyWith(transition: current.copyWith(duration: v))),
              ),
            ),
            SizedBox(width: 56, child: Text('${current.duration.toStringAsFixed(2)}s')),
          ],
        ),
      ],
    );
  }
}
