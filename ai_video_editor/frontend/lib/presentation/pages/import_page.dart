import 'package:file_selector/file_selector.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../application/providers.dart';
import '../shell.dart';
import '../widgets/error_view.dart';
import '../widgets/job_progress.dart';

const _videoTypeGroup = XTypeGroup(
  label: 'Video',
  extensions: ['mp4', 'mov', 'mkv', 'avi', 'webm'],
);

class ImportPage extends ConsumerStatefulWidget {
  const ImportPage({super.key});

  @override
  ConsumerState<ImportPage> createState() => _ImportPageState();
}

class _ImportPageState extends ConsumerState<ImportPage> {
  String? _importJobId;
  String? _creatingError;
  bool _creating = false;

  Future<void> _pickAndImport() async {
    final file = await openFile(acceptedTypeGroups: [_videoTypeGroup]);
    if (file == null) return;
    setState(() {
      _creating = true;
      _creatingError = null;
    });

    final repo = ref.read(repositoryProvider);
    final result = await repo.createProject(file.path, name: file.name);
    result.when(
      ok: (created) {
        setState(() {
          _creating = false;
          _importJobId = created.jobId;
        });
      },
      err: (e) => setState(() {
        _creating = false;
        _creatingError = e.message;
      }),
    );
  }

  void _onProbeDone(Map<String, dynamic>? result) async {
    if (result == null) return;
    final project = ref.read(repositoryProvider);
    final projectId = result['id'] as String;
    final fresh = await project.getProject(projectId);
    fresh.when(
      ok: (p) => ref.read(currentProjectProvider.notifier).setProject(p),
      err: (_) {},
    );
    ref.invalidate(projectsListProvider);
  }

  @override
  Widget build(BuildContext context) {
    final projectState = ref.watch(currentProjectProvider);
    final project = projectState.project;

    return Padding(
      padding: const EdgeInsets.all(24),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text('Import', style: Theme.of(context).textTheme.headlineSmall),
          const SizedBox(height: 16),
          FilledButton.icon(
            onPressed: _creating ? null : _pickAndImport,
            icon: const Icon(Icons.file_open_outlined),
            label: const Text('Choose video file...'),
          ),
          if (_creatingError != null) ...[
            const SizedBox(height: 12),
            Text(_creatingError!, style: const TextStyle(color: Colors.redAccent)),
          ],
          if (_importJobId != null) ...[
            const SizedBox(height: 16),
            JobProgressView(
              jobId: _importJobId!,
              onDone: _onProbeDone,
              onFailed: (e) => setState(() => _creatingError = e),
            ),
          ],
          const SizedBox(height: 24),
          if (project?.video != null) _VideoInfoCard(projectId: project!.id, video: project.video!),
        ],
      ),
    );
  }
}

class _VideoInfoCard extends ConsumerStatefulWidget {
  const _VideoInfoCard({required this.projectId, required this.video});
  final String projectId;
  final dynamic video;

  @override
  ConsumerState<_VideoInfoCard> createState() => _VideoInfoCardState();
}

class _VideoInfoCardState extends ConsumerState<_VideoInfoCard> {
  String? _transcribeJobId;

  @override
  Widget build(BuildContext context) {
    final repo = ref.read(repositoryProvider);
    final video = widget.video;

    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            ClipRRect(
              borderRadius: BorderRadius.circular(6),
              child: Image.network(
                repo.fileUrl(video.thumbnailPath as String),
                width: 200,
                height: 112,
                fit: BoxFit.cover,
                errorBuilder: (_, _, _) => Container(width: 200, height: 112, color: Colors.black26),
              ),
            ),
            const SizedBox(width: 16),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text('${video.width}x${video.height} @ ${video.fps.toStringAsFixed(1)} fps'),
                  Text('Duration: ${video.durationSec.toStringAsFixed(1)}s'),
                  Text('Codec: ${video.codec}  •  Audio: ${video.hasAudio ? "yes" : "no"}'),
                  const SizedBox(height: 12),
                  if (_transcribeJobId == null)
                    FilledButton.icon(
                      onPressed: () async {
                        final result = await repo.transcribe(widget.projectId);
                        result.when(
                          ok: (jobId) => setState(() => _transcribeJobId = jobId),
                          err: (e) => ScaffoldMessenger.of(context)
                              .showSnackBar(SnackBar(content: ErrorView(error: e))),
                        );
                      },
                      icon: const Icon(Icons.mic_outlined),
                      label: const Text('Transcribe'),
                    )
                  else
                    JobProgressView(
                      jobId: _transcribeJobId!,
                      onDone: (result) async {
                        final fresh = await repo.getProject(widget.projectId);
                        fresh.when(
                          ok: (p) {
                            ref.read(currentProjectProvider.notifier).setProject(p);
                            ref.read(selectedNavIndexProvider.notifier).state = 3;
                          },
                          err: (_) {},
                        );
                      },
                      onFailed: (e) => ScaffoldMessenger.of(context)
                          .showSnackBar(SnackBar(content: Text('Transcription failed: $e'))),
                    ),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }
}
