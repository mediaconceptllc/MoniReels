import 'dart:ui' as ui;

import 'package:file_selector/file_selector.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../application/providers.dart';
import '../../../domain/models.dart';
import '../../widgets/job_progress.dart';

const _videoTypeGroup = XTypeGroup(
  label: 'Video',
  extensions: ['mp4', 'mov', 'mkv', 'avi', 'webm'],
);

enum _Stage { idle, creatingProject, probing, transcribing, suggesting, done }

/// Pick a video, then everything else happens automatically: create project
/// -> probe -> transcribe -> AI suggestions. Each stage auto-fires the next
/// via JobProgressView.onDone, mirroring the old Import page's manual
/// probe->transcribe chain but without the button click in between.
class Step1Import extends ConsumerStatefulWidget {
  const Step1Import({super.key, required this.onNext});
  final VoidCallback onNext;

  @override
  ConsumerState<Step1Import> createState() => _Step1ImportState();
}

class _Step1ImportState extends ConsumerState<Step1Import> {
  _Stage _stage = _Stage.idle;
  String? _jobId;
  String? _projectId;
  String? _error;

  Future<void> _pickAndStart() async {
    final file = await openFile(acceptedTypeGroups: [_videoTypeGroup]);
    if (file == null) return;
    setState(() {
      _stage = _Stage.creatingProject;
      _error = null;
    });

    final result = await ref.read(repositoryProvider).createProject(file.path, name: file.name);
    result.when(
      ok: (created) => setState(() {
        _projectId = created.projectId;
        _jobId = created.jobId;
        _stage = _Stage.probing;
      }),
      err: (e) => setState(() {
        _stage = _Stage.idle;
        _error = e.message;
      }),
    );
  }

  Future<void> _onProbeDone(Map<String, dynamic>? result) async {
    if (result == null || _projectId == null) return;
    final fresh = await ref.read(repositoryProvider).getProject(_projectId!);
    fresh.when(ok: (p) => ref.read(currentProjectProvider.notifier).setProject(p), err: (_) {});
    ref.invalidate(projectsListProvider);

    final job = await ref.read(repositoryProvider).transcribe(_projectId!);
    job.when(
      ok: (jobId) => setState(() {
        _jobId = jobId;
        _stage = _Stage.transcribing;
      }),
      err: (e) => setState(() => _error = e.message),
    );
  }

  Future<void> _onTranscribeDone(Map<String, dynamic>? result) async {
    if (_projectId == null) return;
    final fresh = await ref.read(repositoryProvider).getProject(_projectId!);
    fresh.when(ok: (p) => ref.read(currentProjectProvider.notifier).setProject(p), err: (_) {});
    await _startSuggestJob();
  }

  /// Kicks off AI suggestion generation and moves to the suggesting stage —
  /// shared by the normal auto-chain (right after transcribing) and by
  /// "Regenerate suggestions" on an already-done project. [provider]
  /// ('openai'/'anthropic') overrides the Settings default for just this run.
  Future<void> _startSuggestJob({String? provider}) async {
    if (_projectId == null) return;
    final job = await ref.read(repositoryProvider).suggest(_projectId!, provider: provider);
    job.when(
      ok: (jobId) => setState(() {
        _jobId = jobId;
        _stage = _Stage.suggesting;
        _error = null;
      }),
      err: (e) => setState(() => _error = e.message),
    );
  }

  /// Opens a previously-created project and resumes wherever it left off:
  /// straight to the review screen if it already has suggestions, straight
  /// to (re)generating suggestions if it has a transcript but no
  /// suggestions yet, or straight to transcribing if it doesn't even have
  /// that.
  Future<void> _openExistingProject(Project project) async {
    setState(() {
      _projectId = project.id;
      _error = null;
    });
    ref.read(currentProjectProvider.notifier).setProject(project);

    if (project.suggestions != null) {
      setState(() => _stage = _Stage.done);
    } else if (project.transcript != null) {
      await _startSuggestJob();
    } else {
      final job = await ref.read(repositoryProvider).transcribe(project.id);
      job.when(
        ok: (jobId) => setState(() {
          _jobId = jobId;
          _stage = _Stage.transcribing;
        }),
        err: (e) => setState(() => _error = e.message),
      );
    }
  }

  Future<void> _onSuggestDone(Map<String, dynamic>? result) async {
    if (_projectId == null) return;
    final fresh = await ref.read(repositoryProvider).getProject(_projectId!);
    fresh.when(
      ok: (p) {
        ref.read(currentProjectProvider.notifier).setProject(p);
        setState(() => _stage = _Stage.done);
      },
      err: (e) => setState(() => _error = e.message),
    );
  }

  String _stageLabel(_Stage s) => switch (s) {
        _Stage.creatingProject => 'Creating project…',
        _Stage.probing => 'Reading video…',
        _Stage.transcribing => 'Transcribing…',
        _Stage.suggesting => 'Generating AI suggestions…',
        _Stage.done => 'Done',
        _Stage.idle => '',
      };

  void Function(Map<String, dynamic>?) get _onDoneForStage => switch (_stage) {
        _Stage.probing => _onProbeDone,
        _Stage.transcribing => _onTranscribeDone,
        _Stage.suggesting => _onSuggestDone,
        _ => (_) {},
      };

  @override
  Widget build(BuildContext context) {
    final project = ref.watch(currentProjectProvider).project;

    return Padding(
      padding: const EdgeInsets.all(24),
      child: _stage == _Stage.idle
          ? SingleChildScrollView(
              child: Column(
                children: [
                  const SizedBox(height: 24),
                  Column(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      Text('Pick a video to get started', style: Theme.of(context).textTheme.headlineSmall),
                      const SizedBox(height: 8),
                      const Text(
                        "We'll transcribe it and generate short-form ideas automatically.",
                        style: TextStyle(color: Colors.white54),
                      ),
                      const SizedBox(height: 24),
                      FilledButton.icon(
                        onPressed: _pickAndStart,
                        icon: const Icon(Icons.file_open_outlined),
                        label: const Text('Choose video file...'),
                      ),
                      if (_error != null) ...[
                        const SizedBox(height: 12),
                        Text(_error!, style: const TextStyle(color: Colors.redAccent)),
                      ],
                    ],
                  ),
                  _ExistingProjectsSection(onOpen: _openExistingProject),
                ],
              ),
            )
          : Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(_stageLabel(_stage), style: Theme.of(context).textTheme.titleMedium),
                const SizedBox(height: 12),
                if (_jobId != null && _stage != _Stage.done)
                  JobProgressView(
                    key: ValueKey(_jobId),
                    jobId: _jobId!,
                    onDone: _onDoneForStage,
                    onFailed: (e) => setState(() => _error = e),
                  ),
                if (_error != null)
                  Padding(
                    padding: const EdgeInsets.only(top: 12),
                    child: Text(_error!, style: const TextStyle(color: Colors.redAccent)),
                  ),
                if (_stage == _Stage.done && project?.suggestions != null) ...[
                  const SizedBox(height: 16),
                  Expanded(child: _ReviewPanel(project: project!)),
                  const SizedBox(height: 12),
                  Row(
                    mainAxisAlignment: MainAxisAlignment.spaceBetween,
                    children: [
                      Wrap(
                        spacing: 8,
                        runSpacing: 8,
                        children: [
                          OutlinedButton.icon(
                            onPressed: () => _startSuggestJob(provider: 'openai'),
                            icon: const Icon(Icons.refresh),
                            label: const Text('Regenerate with ChatGPT'),
                          ),
                          OutlinedButton.icon(
                            onPressed: () => _startSuggestJob(provider: 'anthropic'),
                            icon: const Icon(Icons.refresh),
                            label: const Text('Regenerate with Claude'),
                          ),
                        ],
                      ),
                      FilledButton(onPressed: widget.onNext, child: const Text('Next')),
                    ],
                  ),
                ],
              ],
            ),
    );
  }
}

/// Transcript editor (fix wrong Chimege text, select lines to turn into a
/// custom reel) above the AI suggestion summary — both operate on the same
/// project, shown together so mistakes can be fixed before Step 2/3.
class _ReviewPanel extends StatelessWidget {
  const _ReviewPanel({required this.project});
  final Project project;

  @override
  Widget build(BuildContext context) {
    final suggestions = project.suggestions!;
    return SingleChildScrollView(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          if (project.transcript != null) ...[
            _TranscriptEditor(project: project),
            const SizedBox(height: 24),
          ],
          Text('${suggestions.shorts.length} reel ideas', style: Theme.of(context).textTheme.titleSmall),
          const SizedBox(height: 8),
          Wrap(
            spacing: 16,
            runSpacing: 16,
            children: [for (final s in suggestions.shorts) _ReelCard(short: s)],
          ),
          const SizedBox(height: 24),
          if (suggestions.youtube.isNotEmpty) ...[
            Text('${suggestions.youtube.length} YouTube ideas', style: Theme.of(context).textTheme.titleSmall),
            const SizedBox(height: 8),
            Wrap(
              spacing: 16,
              runSpacing: 16,
              children: [for (final y in suggestions.youtube) _YoutubeSummaryCard(plan: y)],
            ),
          ] else
            const Text(
              'No YouTube-length ideas — this video is under 20 minutes.',
              style: TextStyle(color: Colors.white54),
            ),
        ],
      ),
    );
  }
}

class _TranscriptEditor extends ConsumerStatefulWidget {
  const _TranscriptEditor({required this.project});
  final Project project;

  @override
  ConsumerState<_TranscriptEditor> createState() => _TranscriptEditorState();
}

class _TranscriptEditorState extends ConsumerState<_TranscriptEditor> {
  final Set<String> _selectedIds = {};

  void _createReelFromSelection() {
    final segments = widget.project.transcript!.segments;
    final selected = segments.where((s) => _selectedIds.contains(s.id)).toList()
      ..sort((a, b) => a.start.compareTo(b.start));
    if (selected.isEmpty) return;

    final text = selected.map((s) => s.text).join(' ').trim();
    final title = text.isEmpty
        ? 'Custom clip'
        : (text.length > 40 ? '${text.substring(0, 40)}…' : text);

    final newReel = ShortIdea(
      id: 'custom_${DateTime.now().microsecondsSinceEpoch}',
      title: title,
      hookText: title,
      hookQuote: text.length > 60 ? text.substring(0, 60) : text,
      cuts: [
        Cut(
          start: selected.first.start,
          end: selected.last.end,
          role: 'hook',
          reason: 'Manually selected from the transcript',
        ),
      ],
      caption: text,
      whyItWorks: 'Manually selected by the editor',
    );

    ref.read(currentProjectProvider.notifier).update((p) {
      final current = p.suggestions ?? Suggestions(shorts: const []);
      return p.copyWith(
        suggestions: Suggestions(shorts: [...current.shorts, newReel], youtube: current.youtube),
      );
    });

    setState(() => _selectedIds.clear());
    ScaffoldMessenger.of(context)
        .showSnackBar(const SnackBar(content: Text('Reel added below — from your selected lines')));
  }

  void _commitEdit(Segment segment, String newText) {
    if (newText == segment.text) return;
    ref.read(currentProjectProvider.notifier).update((p) {
      final transcript = p.transcript!;
      final updated = [
        for (final s in transcript.segments)
          if (s.id == segment.id) s.copyWith(text: newText) else s,
      ];
      return p.copyWith(transcript: transcript.copyWith(segments: updated));
    });
  }

  @override
  Widget build(BuildContext context) {
    final segments = widget.project.transcript!.segments;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Row(
          children: [
            Text('Transcript', style: Theme.of(context).textTheme.titleSmall),
            const Spacer(),
            if (_selectedIds.isNotEmpty)
              FilledButton.icon(
                onPressed: _createReelFromSelection,
                icon: const Icon(Icons.add),
                label: Text('Create reel from ${_selectedIds.length} selected'),
              ),
          ],
        ),
        const SizedBox(height: 4),
        const Text(
          'Fix any wrong transcription text, or check a few lines to turn them into your own reel.',
          style: TextStyle(color: Colors.white54, fontSize: 12),
        ),
        const SizedBox(height: 8),
        Container(
          constraints: const BoxConstraints(maxHeight: 260),
          decoration: BoxDecoration(border: Border.all(color: Colors.white12), borderRadius: BorderRadius.circular(8)),
          child: segments.isEmpty
              ? const Padding(
                  padding: EdgeInsets.all(16),
                  child: Text('No transcript.', style: TextStyle(color: Colors.white54)),
                )
              : ListView.builder(
                  shrinkWrap: true,
                  itemCount: segments.length,
                  itemBuilder: (context, i) => _SegmentRow(
                    segment: segments[i],
                    selected: _selectedIds.contains(segments[i].id),
                    onSelectChanged: (v) => setState(() {
                      if (v) {
                        _selectedIds.add(segments[i].id);
                      } else {
                        _selectedIds.remove(segments[i].id);
                      }
                    }),
                    onTextCommitted: (text) => _commitEdit(segments[i], text),
                  ),
                ),
        ),
      ],
    );
  }
}

class _SegmentRow extends StatefulWidget {
  const _SegmentRow({
    required this.segment,
    required this.selected,
    required this.onSelectChanged,
    required this.onTextCommitted,
  });
  final Segment segment;
  final bool selected;
  final ValueChanged<bool> onSelectChanged;
  final ValueChanged<String> onTextCommitted;

  @override
  State<_SegmentRow> createState() => _SegmentRowState();
}

class _SegmentRowState extends State<_SegmentRow> {
  bool _editing = false;
  late final TextEditingController _controller = TextEditingController(text: widget.segment.text);

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  void _startEditing() {
    _controller.text = widget.segment.text; // resync in case an earlier commit changed it
    setState(() => _editing = true);
  }

  void _commit() {
    setState(() => _editing = false);
    widget.onTextCommitted(_controller.text);
  }

  @override
  Widget build(BuildContext context) {
    return ListTile(
      dense: true,
      leading: Checkbox(value: widget.selected, onChanged: (v) => widget.onSelectChanged(v ?? false)),
      title: _editing
          ? TextField(
              controller: _controller,
              autofocus: true,
              style: const TextStyle(fontSize: 13),
              onSubmitted: (_) => _commit(),
              onTapOutside: (_) => _commit(),
            )
          : Text(widget.segment.text, style: const TextStyle(fontSize: 13)),
      subtitle: Text(
        '${widget.segment.start.toStringAsFixed(1)}s - ${widget.segment.end.toStringAsFixed(1)}s',
        style: const TextStyle(fontSize: 10, color: Colors.white38),
      ),
      trailing: _editing
          ? IconButton(icon: const Icon(Icons.check, size: 18), onPressed: _commit)
          : IconButton(icon: const Icon(Icons.edit_outlined, size: 18), onPressed: _startEditing),
    );
  }
}

/// Lets the user reopen a project created in an earlier session instead of
/// always starting fresh — resumes wherever it left off (see
/// _Step1ImportState._openExistingProject) and can regenerate suggestions
/// for one that already has them.
class _ExistingProjectsSection extends ConsumerWidget {
  const _ExistingProjectsSection({required this.onOpen});
  final ValueChanged<Project> onOpen;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final projects = ref.watch(projectsListProvider);
    return projects.when(
      data: (list) {
        if (list.isEmpty) return const SizedBox.shrink();
        return Padding(
          padding: const EdgeInsets.only(top: 40),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              const Divider(),
              const SizedBox(height: 12),
              Text('Or open an existing project', style: Theme.of(context).textTheme.titleSmall),
              const SizedBox(height: 12),
              SizedBox(
                height: 172,
                child: ListView.separated(
                  scrollDirection: Axis.horizontal,
                  itemCount: list.length,
                  separatorBuilder: (_, _) => const SizedBox(width: 12),
                  itemBuilder: (context, i) => _ExistingProjectCard(project: list[i], onTap: () => onOpen(list[i])),
                ),
              ),
            ],
          ),
        );
      },
      loading: () => const SizedBox.shrink(),
      error: (_, _) => const SizedBox.shrink(),
    );
  }
}

class _ExistingProjectCard extends ConsumerWidget {
  const _ExistingProjectCard({required this.project, required this.onTap});
  final Project project;
  final VoidCallback onTap;

  String get _status {
    if (project.suggestions != null) return 'Has suggestions';
    if (project.transcript != null) return 'Transcribed';
    return 'Not transcribed';
  }

  Future<void> _confirmDelete(BuildContext context, WidgetRef ref) async {
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('Delete project?'),
        content: Text('"${project.name}" and all its files will be permanently deleted. This cannot be undone.'),
        actions: [
          TextButton(onPressed: () => Navigator.of(context).pop(false), child: const Text('Cancel')),
          FilledButton(
            onPressed: () => Navigator.of(context).pop(true),
            style: FilledButton.styleFrom(backgroundColor: Colors.redAccent),
            child: const Text('Delete'),
          ),
        ],
      ),
    );
    if (confirmed != true || !context.mounted) return;

    final result = await ref.read(repositoryProvider).deleteProject(project.id);
    result.when(
      ok: (_) => ref.invalidate(projectsListProvider),
      err: (e) {
        if (!context.mounted) return;
        ScaffoldMessenger.of(context)
            .showSnackBar(SnackBar(content: Text('Delete failed: ${e.message}')));
      },
    );
  }

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final repo = ref.read(repositoryProvider);
    final thumbnailPath = project.video?.thumbnailPath;
    return SizedBox(
      width: 180,
      child: Card(
        clipBehavior: ui.Clip.antiAlias,
        child: Stack(
          children: [
            InkWell(
              onTap: onTap,
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  ClipRRect(
                    borderRadius: const BorderRadius.vertical(top: Radius.circular(4)),
                    child: AspectRatio(
                      aspectRatio: 16 / 9,
                      child: thumbnailPath == null
                          ? Container(color: Colors.black26)
                          : Image.network(
                              repo.fileUrl(thumbnailPath),
                              fit: BoxFit.cover,
                              errorBuilder: (_, _, _) => Container(color: Colors.black26),
                            ),
                    ),
                  ),
                  Padding(
                    padding: const EdgeInsets.all(8),
                    child: Column(
                      mainAxisSize: MainAxisSize.min,
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text(project.name, maxLines: 1, overflow: TextOverflow.ellipsis),
                        const SizedBox(height: 2),
                        Text(
                          _status,
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                          style: const TextStyle(fontSize: 11, color: Colors.white54),
                        ),
                      ],
                    ),
                  ),
                ],
              ),
            ),
            Positioned(
              top: 2,
              right: 2,
              child: Material(
                color: Colors.black45,
                shape: const CircleBorder(),
                child: IconButton(
                  onPressed: () => _confirmDelete(context, ref),
                  icon: const Icon(Icons.delete_outline, size: 16),
                  color: Colors.white,
                  tooltip: 'Delete project',
                  constraints: const BoxConstraints(minWidth: 28, minHeight: 28),
                  padding: EdgeInsets.zero,
                  visualDensity: VisualDensity.compact,
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _ReelCard extends StatelessWidget {
  const _ReelCard({required this.short});
  final ShortIdea short;

  @override
  Widget build(BuildContext context) {
    return SizedBox(
      width: 260,
      child: Card(
        child: Padding(
          padding: const EdgeInsets.all(16),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Row(
                children: [
                  Expanded(child: Text(short.title, style: Theme.of(context).textTheme.titleMedium)),
                  Chip(label: Text('${short.duration.toStringAsFixed(0)}s'), visualDensity: VisualDensity.compact),
                ],
              ),
              const SizedBox(height: 8),
              Text(short.hookText, style: const TextStyle(fontStyle: FontStyle.italic, color: Colors.white70)),
              const SizedBox(height: 8),
              Text(short.caption, maxLines: 3, overflow: TextOverflow.ellipsis),
              const SizedBox(height: 8),
              Text(
                '${short.cuts.length} cuts',
                style: const TextStyle(fontSize: 11, color: Colors.white54),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

class _YoutubeSummaryCard extends StatelessWidget {
  const _YoutubeSummaryCard({required this.plan});
  final YoutubePlan plan;

  @override
  Widget build(BuildContext context) {
    return SizedBox(
      width: 300,
      child: Card(
        child: Padding(
          padding: const EdgeInsets.all(16),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Row(
                children: [
                  Expanded(child: Text(plan.title, style: Theme.of(context).textTheme.titleMedium)),
                  Chip(label: Text('${plan.totalDuration.toStringAsFixed(0)}s')),
                ],
              ),
              const SizedBox(height: 8),
              Text(plan.throughline, maxLines: 3, overflow: TextOverflow.ellipsis),
              const SizedBox(height: 8),
              Text('${plan.ranges.length} keep-ranges', style: const TextStyle(color: Colors.white54)),
            ],
          ),
        ),
      ),
    );
  }
}
