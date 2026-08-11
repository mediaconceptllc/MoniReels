import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../application/providers.dart';
import '../../domain/models.dart';
import '../../domain/transition_registry.dart';

class TransitionsPage extends ConsumerWidget {
  const TransitionsPage({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final projectState = ref.watch(currentProjectProvider);
    final project = projectState.project;
    final capabilities = ref.watch(capabilitiesProvider);

    if (project == null) {
      return const Center(child: Text('No project loaded.', style: TextStyle(color: Colors.white54)));
    }

    return Padding(
      padding: const EdgeInsets.all(24),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text('Transitions', style: Theme.of(context).textTheme.headlineSmall),
          const SizedBox(height: 16),
          Expanded(
            child: capabilities.when(
              data: (caps) => _TransitionGrid(project: project, supportedXfade: caps.xfadeTransitions),
              loading: () => const Center(child: CircularProgressIndicator()),
              error: (e, _) => Text('Could not load capabilities: $e'),
            ),
          ),
        ],
      ),
    );
  }
}

class _TransitionGrid extends ConsumerWidget {
  const _TransitionGrid({required this.project, required this.supportedXfade});
  final Project project;
  final List<String> supportedXfade;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final current = project.transition;

    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Expanded(
          child: GridView.builder(
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
        FilledButton.icon(
          onPressed: () => ref.read(currentProjectProvider.notifier).update((p) => p.copyWith(
                clips: [for (final c in p.clips) c.copyWith(transitionIn: '')],
              )),
          icon: const Icon(Icons.done_all),
          label: const Text('Apply to all joins (clear per-join overrides)'),
        ),
      ],
    );
  }
}
