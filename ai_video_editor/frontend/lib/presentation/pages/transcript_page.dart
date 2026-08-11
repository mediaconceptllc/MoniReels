import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../application/providers.dart';
import '../../application/video_player_controller.dart';
import '../../domain/models.dart';
import '../widgets/video_preview.dart';

String _mmss(double seconds) {
  final total = seconds.round();
  final m = total ~/ 60;
  final s = total % 60;
  return '${m.toString().padLeft(2, '0')}:${s.toString().padLeft(2, '0')}';
}

class TranscriptPage extends ConsumerStatefulWidget {
  const TranscriptPage({super.key});

  @override
  ConsumerState<TranscriptPage> createState() => _TranscriptPageState();
}

class _TranscriptPageState extends ConsumerState<TranscriptPage> {
  final _searchController = TextEditingController();
  String _query = '';

  @override
  void dispose() {
    _searchController.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final projectState = ref.watch(currentProjectProvider);
    final project = projectState.project;

    if (project == null) {
      return const Center(child: Text('No project loaded.', style: TextStyle(color: Colors.white54)));
    }
    if (project.transcript == null) {
      return const Center(
        child: Text('No transcript yet — run Transcribe from the Import page.', style: TextStyle(color: Colors.white54)),
      );
    }

    final segments = project.transcript!.segments;
    final filtered = _query.isEmpty
        ? segments
        : segments.where((s) => s.text.toLowerCase().contains(_query.toLowerCase())).toList();

    return Padding(
      padding: const EdgeInsets.all(24),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Expanded(
            flex: 3,
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text('Transcript', style: Theme.of(context).textTheme.headlineSmall),
                const SizedBox(height: 12),
                TextField(
                  controller: _searchController,
                  decoration: const InputDecoration(prefixIcon: Icon(Icons.search), hintText: 'Search transcript...'),
                  onChanged: (v) => setState(() => _query = v),
                ),
                const SizedBox(height: 12),
                Expanded(
                  child: ListView.builder(
                    itemCount: filtered.length,
                    itemBuilder: (context, i) => _SegmentTile(segment: filtered[i]),
                  ),
                ),
              ],
            ),
          ),
          const SizedBox(width: 24),
          Expanded(
            flex: 2,
            child: AspectRatio(
              aspectRatio: 16 / 9,
              child: VideoPreview(sourcePath: project.video?.path),
            ),
          ),
        ],
      ),
    );
  }
}

class _SegmentTile extends ConsumerStatefulWidget {
  const _SegmentTile({required this.segment});
  final Segment segment;

  @override
  ConsumerState<_SegmentTile> createState() => _SegmentTileState();
}

class _SegmentTileState extends ConsumerState<_SegmentTile> {
  bool _editing = false;
  late final TextEditingController _textController = TextEditingController(text: widget.segment.text);

  @override
  void dispose() {
    _textController.dispose();
    super.dispose();
  }

  void _commitEdit() {
    final newText = _textController.text;
    setState(() => _editing = false);
    if (newText == widget.segment.text) return;
    ref.read(currentProjectProvider.notifier).update((project) {
      final transcript = project.transcript!;
      final updatedSegments = [
        for (final s in transcript.segments)
          if (s.id == widget.segment.id) s.copyWith(text: newText) else s,
      ];
      return project.copyWith(transcript: transcript.copyWith(segments: updatedSegments));
    });
  }

  @override
  Widget build(BuildContext context) {
    return ListTile(
      dense: true,
      leading: InkWell(
        onTap: () => ref
            .read(previewPlayerProvider)
            .seekTo(Duration(milliseconds: (widget.segment.start * 1000).round())),
        child: Chip(label: Text(_mmss(widget.segment.start))),
      ),
      title: _editing
          ? TextField(
              controller: _textController,
              autofocus: true,
              onSubmitted: (_) => _commitEdit(),
              onTapOutside: (_) => _commitEdit(),
            )
          : Text(widget.segment.text),
      trailing: _editing
          ? IconButton(icon: const Icon(Icons.check, size: 18), onPressed: _commitEdit)
          : IconButton(
              icon: const Icon(Icons.edit_outlined, size: 18),
              onPressed: () => setState(() => _editing = true),
            ),
    );
  }
}
