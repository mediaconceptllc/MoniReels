import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:media_kit/media_kit.dart';
import 'package:media_kit_video/media_kit_video.dart';

/// One shared player instance for the whole app — Transcript and Timeline
/// pages both seek into it rather than each owning their own player.
final playerProvider = Provider<Player>((ref) {
  final player = Player();
  ref.onDispose(player.dispose);
  return player;
});

final videoControllerProvider = Provider<VideoController>((ref) {
  final player = ref.watch(playerProvider);
  return VideoController(player);
});

class PreviewPlayer {
  PreviewPlayer(this._player);
  final Player _player;

  String? _openedPath;

  Future<void> ensureOpen(String sourcePath) async {
    if (_openedPath == sourcePath) return;
    _openedPath = sourcePath;
    await _player.open(Media(sourcePath), play: false);
  }

  Future<void> seekTo(Duration position) => _player.seek(position);

  Future<void> playPause() => _player.playOrPause();
}

final previewPlayerProvider = Provider<PreviewPlayer>((ref) => PreviewPlayer(ref.watch(playerProvider)));
