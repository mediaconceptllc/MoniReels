import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:media_kit_video/media_kit_video.dart';

import '../../application/video_player_controller.dart';

/// Opens [sourcePath] (a local file path) in the shared player and shows it.
class VideoPreview extends ConsumerStatefulWidget {
  const VideoPreview({super.key, required this.sourcePath});

  final String? sourcePath;

  @override
  ConsumerState<VideoPreview> createState() => _VideoPreviewState();
}

class _VideoPreviewState extends ConsumerState<VideoPreview> {
  @override
  void didUpdateWidget(covariant VideoPreview oldWidget) {
    super.didUpdateWidget(oldWidget);
    _openIfNeeded();
  }

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) => _openIfNeeded());
  }

  void _openIfNeeded() {
    final path = widget.sourcePath;
    if (path == null) return;
    ref.read(previewPlayerProvider).ensureOpen(path);
  }

  @override
  Widget build(BuildContext context) {
    if (widget.sourcePath == null) {
      return const ColoredBox(
        color: Colors.black,
        child: Center(child: Text('No video loaded', style: TextStyle(color: Colors.white38))),
      );
    }
    final controller = ref.watch(videoControllerProvider);
    return ColoredBox(color: Colors.black, child: Video(controller: controller));
  }
}
