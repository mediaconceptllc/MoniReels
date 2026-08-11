import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../application/providers.dart';
import '../../domain/models.dart';
import '../shell.dart';
import '../widgets/job_progress.dart';

String _localId(int salt) => '${DateTime.now().microsecondsSinceEpoch}_$salt';

class SuggestionsPage extends ConsumerStatefulWidget {
  const SuggestionsPage({super.key});

  @override
  ConsumerState<SuggestionsPage> createState() => _SuggestionsPageState();
}

class _SuggestionsPageState extends ConsumerState<SuggestionsPage> {
  String? _jobId;
  String? _error;

  Future<void> _generate(String projectId) async {
    setState(() {
      _error = null;
    });
    final result = await ref.read(repositoryProvider).suggest(projectId);
    result.when(
      ok: (jobId) => setState(() => _jobId = jobId),
      err: (e) => setState(() => _error = e.message),
    );
  }

  void _useShort(Project project, ShortIdea short) {
    final clip = Clip(
      id: _localId(0),
      sourcePath: project.video!.path,
      start: short.start,
      end: short.end,
      order: 0,
    );
    ref.read(currentProjectProvider.notifier).update((p) => p.copyWith(clips: [clip]));
    ref.read(selectedNavIndexProvider.notifier).state = 5;
  }

  void _useYoutubePlan(Project project, YoutubePlan plan) {
    final clips = [
      for (var i = 0; i < plan.ranges.length; i++)
        Clip(
          id: _localId(i),
          sourcePath: project.video!.path,
          start: plan.ranges[i].start,
          end: plan.ranges[i].end,
          order: i,
        ),
    ];
    ref.read(currentProjectProvider.notifier).update((p) => p.copyWith(clips: clips));
    ref.read(selectedNavIndexProvider.notifier).state = 5;
  }

  @override
  Widget build(BuildContext context) {
    final projectState = ref.watch(currentProjectProvider);
    final project = projectState.project;

    if (project == null) {
      return const Center(child: Text('No project loaded.', style: TextStyle(color: Colors.white54)));
    }

    return Padding(
      padding: const EdgeInsets.all(24),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Text('AI Suggestions', style: Theme.of(context).textTheme.headlineSmall),
              const Spacer(),
              FilledButton.icon(
                onPressed: project.transcript == null ? null : () => _generate(project.id),
                icon: const Icon(Icons.auto_awesome),
                label: const Text('Generate Suggestions'),
              ),
            ],
          ),
          if (project.transcript == null)
            const Padding(
              padding: EdgeInsets.only(top: 8),
              child: Text('Transcribe the video first.', style: TextStyle(color: Colors.white54)),
            ),
          if (_error != null)
            Padding(padding: const EdgeInsets.only(top: 8), child: Text(_error!, style: const TextStyle(color: Colors.redAccent))),
          if (_jobId != null)
            Padding(
              padding: const EdgeInsets.symmetric(vertical: 12),
              child: JobProgressView(
                jobId: _jobId!,
                onDone: (_) async {
                  final fresh = await ref.read(repositoryProvider).getProject(project.id);
                  fresh.when(ok: (p) => ref.read(currentProjectProvider.notifier).setProject(p), err: (_) {});
                },
                onFailed: (e) => setState(() => _error = e),
              ),
            ),
          const SizedBox(height: 16),
          Expanded(
            child: project.suggestions == null
                ? const Center(child: Text('No suggestions yet.', style: TextStyle(color: Colors.white54)))
                : ListView(
                    children: [
                      Wrap(
                        spacing: 16,
                        runSpacing: 16,
                        children: [
                          for (final short in project.suggestions!.shorts)
                            _ShortCard(short: short, onUse: () => _useShort(project, short)),
                        ],
                      ),
                      if (project.suggestions!.youtube != null) ...[
                        const SizedBox(height: 24),
                        _YoutubeCard(
                          plan: project.suggestions!.youtube!,
                          onUse: () => _useYoutubePlan(project, project.suggestions!.youtube!),
                        ),
                      ],
                    ],
                  ),
          ),
        ],
      ),
    );
  }
}

class _ShortCard extends StatelessWidget {
  const _ShortCard({required this.short, required this.onUse});
  final ShortIdea short;
  final VoidCallback onUse;

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
                  Expanded(child: Text(short.title, style: Theme.of(context).textTheme.titleMedium)),
                  Chip(label: Text('${short.duration.toStringAsFixed(0)}s'), visualDensity: VisualDensity.compact),
                ],
              ),
              const SizedBox(height: 8),
              Text(short.hook, style: const TextStyle(fontStyle: FontStyle.italic, color: Colors.white70)),
              const SizedBox(height: 8),
              Text(short.description, maxLines: 3, overflow: TextOverflow.ellipsis),
              const SizedBox(height: 12),
              Text(
                '${short.start.toStringAsFixed(1)}s - ${short.end.toStringAsFixed(1)}s',
                style: const TextStyle(color: Colors.white54, fontSize: 12),
              ),
              const SizedBox(height: 8),
              FilledButton(onPressed: onUse, child: const Text('Use this')),
            ],
          ),
        ),
      ),
    );
  }
}

class _YoutubeCard extends StatelessWidget {
  const _YoutubeCard({required this.plan, required this.onUse});
  final YoutubePlan plan;
  final VoidCallback onUse;

  @override
  Widget build(BuildContext context) {
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Expanded(child: Text(plan.title, style: Theme.of(context).textTheme.titleMedium)),
                Chip(label: Text('${plan.totalDuration.toStringAsFixed(0)}s total')),
              ],
            ),
            const SizedBox(height: 8),
            Text(plan.description),
            const SizedBox(height: 12),
            Text('${plan.ranges.length} keep-ranges', style: const TextStyle(color: Colors.white54)),
            for (final r in plan.ranges)
              Padding(
                padding: const EdgeInsets.only(top: 4),
                child: Text(
                  '${r.start.toStringAsFixed(1)}s - ${r.end.toStringAsFixed(1)}s: ${r.reason}',
                  style: const TextStyle(fontSize: 12, color: Colors.white70),
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                ),
              ),
            const SizedBox(height: 12),
            FilledButton(onPressed: onUse, child: const Text('Use this')),
          ],
        ),
      ),
    );
  }
}
