import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../application/providers.dart';
import '../../application/video_player_controller.dart';
import '../../domain/models.dart';
import '../../domain/transition_registry.dart';
import '../widgets/video_preview.dart';

const double _pxPerSecond = 40.0;
const double _minClipDuration = 0.5;

class TimelinePage extends ConsumerWidget {
  const TimelinePage({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final projectState = ref.watch(currentProjectProvider);
    final project = projectState.project;

    if (project == null) {
      return const Center(child: Text('No project loaded.', style: TextStyle(color: Colors.white54)));
    }

    final clips = [...project.clips]..sort((a, b) => a.order.compareTo(b.order));
    final sourceDuration = project.video?.durationSec ?? double.infinity;

    return Padding(
      padding: const EdgeInsets.all(24),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text('Timeline', style: Theme.of(context).textTheme.headlineSmall),
          const SizedBox(height: 12),
          SizedBox(
            height: 220,
            child: AspectRatio(aspectRatio: 16 / 9, child: VideoPreview(sourcePath: project.video?.path)),
          ),
          const SizedBox(height: 16),
          if (clips.isEmpty)
            const Text('No clips yet — pick a short from AI Suggestions, or add ranges manually.',
                style: TextStyle(color: Colors.white54))
          else
            Expanded(
              child: SingleChildScrollView(
                scrollDirection: Axis.horizontal,
                child: _ClipTrack(clips: clips, sourceDuration: sourceDuration),
              ),
            ),
        ],
      ),
    );
  }
}

class _ClipTrack extends ConsumerWidget {
  const _ClipTrack({required this.clips, required this.sourceDuration});
  final List<Clip> clips;
  final double sourceDuration;

  void _reorder(WidgetRef ref, String draggedId, int dropIndex) {
    ref.read(currentProjectProvider.notifier).update((project) {
      final current = [...project.clips]..sort((a, b) => a.order.compareTo(b.order));
      final draggedIndex = current.indexWhere((c) => c.id == draggedId);
      if (draggedIndex == -1) return project;
      final dragged = current.removeAt(draggedIndex);
      var target = dropIndex;
      if (draggedIndex < dropIndex) target -= 1;
      current.insert(target.clamp(0, current.length), dragged);
      final renumbered = [for (var i = 0; i < current.length; i++) current[i].copyWith(order: i)];
      return project.copyWith(clips: renumbered);
    });
  }

  void _deleteClip(WidgetRef ref, String clipId) {
    ref.read(currentProjectProvider.notifier).update((project) {
      final remaining = project.clips.where((c) => c.id != clipId).toList()
        ..sort((a, b) => a.order.compareTo(b.order));
      final renumbered = [for (var i = 0; i < remaining.length; i++) remaining[i].copyWith(order: i)];
      return project.copyWith(clips: renumbered);
    });
  }

  void _trimClip(WidgetRef ref, String clipId, {double? start, double? end}) {
    ref.read(currentProjectProvider.notifier).update((project) {
      final updated = [
        for (final c in project.clips)
          if (c.id == clipId) c.copyWith(start: start, end: end) else c,
      ];
      return project.copyWith(clips: updated);
    });
  }

  void _setJoinTransition(WidgetRef ref, String clipId, String? transitionType) {
    ref.read(currentProjectProvider.notifier).update((project) {
      final updated = [
        for (final c in project.clips)
          if (c.id == clipId) c.copyWith(transitionIn: transitionType ?? '') else c,
      ];
      return project.copyWith(clips: updated);
    });
  }

  Future<void> _editJoinTransition(BuildContext context, WidgetRef ref, Clip afterClip) async {
    final choice = await showDialog<String>(
      context: context,
      builder: (context) => SimpleDialog(
        title: const Text('Transition for this join'),
        children: [
          SimpleDialogOption(
            onPressed: () => Navigator.pop(context, ''),
            child: const Text('Use project default'),
          ),
          for (final t in kTransitionRegistry)
            SimpleDialogOption(
              onPressed: () => Navigator.pop(context, t.uiName),
              child: Text(t.uiName),
            ),
        ],
      ),
    );
    if (choice == null) return;
    _setJoinTransition(ref, afterClip.id, choice.isEmpty ? null : choice);
  }

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final widgets = <Widget>[_DropZone(index: 0, onAccept: (id) => _reorder(ref, id, 0))];
    for (var i = 0; i < clips.length; i++) {
      final clip = clips[i];
      widgets.add(_ClipBlock(
        clip: clip,
        sourceDuration: sourceDuration,
        onDelete: () => _deleteClip(ref, clip.id),
        onTrim: (start, end) => _trimClip(ref, clip.id, start: start, end: end),
      ));
      if (i < clips.length - 1) {
        widgets.add(_GapWidget(
          transitionLabel: (clip.transitionIn == null || clip.transitionIn!.isEmpty) ? null : clip.transitionIn,
          onTap: () => _editJoinTransition(context, ref, clips[i + 1]),
        ));
      }
      widgets.add(_DropZone(index: i + 1, onAccept: (id) => _reorder(ref, id, i + 1)));
    }
    return Row(crossAxisAlignment: CrossAxisAlignment.start, children: widgets);
  }
}

class _DropZone extends StatelessWidget {
  const _DropZone({required this.index, required this.onAccept});
  final int index;
  final void Function(String draggedClipId) onAccept;

  @override
  Widget build(BuildContext context) {
    return DragTarget<String>(
      onAcceptWithDetails: (details) => onAccept(details.data),
      builder: (context, candidates, rejected) => Container(
        width: candidates.isEmpty ? 6 : 16,
        height: 140,
        color: candidates.isEmpty ? Colors.transparent : Theme.of(context).colorScheme.primary.withValues(alpha: 0.4),
      ),
    );
  }
}

class _GapWidget extends StatelessWidget {
  const _GapWidget({required this.onTap, this.transitionLabel});
  final VoidCallback onTap;
  final String? transitionLabel;

  @override
  Widget build(BuildContext context) {
    return InkWell(
      onTap: onTap,
      child: Container(
        width: 32,
        height: 140,
        alignment: Alignment.center,
        child: Tooltip(
          message: transitionLabel == null ? 'Click to set transition' : 'Transition: $transitionLabel',
          child: Icon(
            Icons.compare_arrows,
            size: 18,
            color: transitionLabel == null ? Colors.white38 : Theme.of(context).colorScheme.primary,
          ),
        ),
      ),
    );
  }
}

class _ClipBlock extends ConsumerWidget {
  const _ClipBlock({
    required this.clip,
    required this.sourceDuration,
    required this.onDelete,
    required this.onTrim,
  });

  final Clip clip;
  final double sourceDuration;
  final void Function() onDelete;
  final void Function(double? start, double? end) onTrim;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final width = (clip.duration * _pxPerSecond).clamp(60.0, 4000.0);

    final content = Container(
      width: width,
      height: 140,
      decoration: BoxDecoration(
        color: const Color(0xFF25222C),
        borderRadius: BorderRadius.circular(6),
        border: Border.all(color: Colors.white24),
      ),
      child: Stack(
        children: [
          Positioned.fill(
            child: Padding(
              padding: const EdgeInsets.all(8),
              child: Column(
                mainAxisAlignment: MainAxisAlignment.center,
                children: [
                  Text('${clip.duration.toStringAsFixed(1)}s', style: const TextStyle(fontWeight: FontWeight.bold)),
                  Text(
                    '${clip.start.toStringAsFixed(1)}-${clip.end.toStringAsFixed(1)}',
                    style: const TextStyle(fontSize: 11, color: Colors.white54),
                  ),
                ],
              ),
            ),
          ),
          Positioned(
            top: 2,
            right: 2,
            child: IconButton(
              icon: const Icon(Icons.close, size: 14),
              padding: EdgeInsets.zero,
              constraints: const BoxConstraints(minWidth: 24, minHeight: 24),
              onPressed: onDelete,
            ),
          ),
          // Left trim handle.
          Positioned(
            left: 0,
            top: 0,
            bottom: 0,
            child: _TrimHandle(
              onDragDelta: (dx) {
                final newStart = (clip.start + dx / _pxPerSecond).clamp(0.0, clip.end - _minClipDuration);
                onTrim(newStart, null);
              },
            ),
          ),
          // Right trim handle.
          Positioned(
            right: 0,
            top: 0,
            bottom: 0,
            child: _TrimHandle(
              onDragDelta: (dx) {
                final newEnd =
                    (clip.end + dx / _pxPerSecond).clamp(clip.start + _minClipDuration, sourceDuration);
                onTrim(null, newEnd);
              },
            ),
          ),
        ],
      ),
    );

    return GestureDetector(
      onTap: () => ref
          .read(previewPlayerProvider)
          .seekTo(Duration(milliseconds: (clip.start * 1000).round())),
      child: LongPressDraggable<String>(
        data: clip.id,
        feedback: Opacity(opacity: 0.7, child: Material(color: Colors.transparent, child: content)),
        childWhenDragging: Opacity(opacity: 0.3, child: content),
        child: content,
      ),
    );
  }
}

class _TrimHandle extends StatelessWidget {
  const _TrimHandle({required this.onDragDelta});
  final void Function(double dx) onDragDelta;

  @override
  Widget build(BuildContext context) {
    return MouseRegion(
      cursor: SystemMouseCursors.resizeLeftRight,
      child: GestureDetector(
        behavior: HitTestBehavior.translucent,
        onHorizontalDragUpdate: (details) => onDragDelta(details.delta.dx),
        child: Container(width: 10, color: Colors.white.withValues(alpha: 0.15)),
      ),
    );
  }
}
