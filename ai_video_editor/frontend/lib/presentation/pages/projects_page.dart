import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../application/providers.dart';
import '../../domain/models.dart';
import '../shell.dart';
import '../widgets/error_view.dart';

class ProjectsPage extends ConsumerWidget {
  const ProjectsPage({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final projects = ref.watch(projectsListProvider);

    return Padding(
      padding: const EdgeInsets.all(24),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Text('Projects', style: Theme.of(context).textTheme.headlineSmall),
              const Spacer(),
              FilledButton.icon(
                onPressed: () {
                  ref.read(currentProjectProvider.notifier).clear();
                  ref.read(selectedNavIndexProvider.notifier).state = 2;
                },
                icon: const Icon(Icons.add),
                label: const Text('New Project'),
              ),
            ],
          ),
          const SizedBox(height: 16),
          Expanded(
            child: projects.when(
              data: (list) => list.isEmpty
                  ? const Center(child: Text('No projects yet.', style: TextStyle(color: Colors.white54)))
                  : GridView.builder(
                      gridDelegate: const SliverGridDelegateWithMaxCrossAxisExtent(
                        maxCrossAxisExtent: 320,
                        mainAxisExtent: 140,
                        crossAxisSpacing: 12,
                        mainAxisSpacing: 12,
                      ),
                      itemCount: list.length,
                      itemBuilder: (context, i) => _ProjectCard(project: list[i]),
                    ),
              loading: () => const Center(child: CircularProgressIndicator()),
              error: (e, _) => Center(child: Text('$e')),
            ),
          ),
        ],
      ),
    );
  }
}

class _ProjectCard extends ConsumerWidget {
  const _ProjectCard({required this.project});
  final Project project;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    return Card(
      child: InkWell(
        onTap: () {
          ref.read(currentProjectProvider.notifier).setProject(project);
          ref.read(selectedNavIndexProvider.notifier).state = 2;
        },
        child: Padding(
          padding: const EdgeInsets.all(16),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Row(
                children: [
                  Expanded(
                    child: Text(project.name, style: Theme.of(context).textTheme.titleMedium, overflow: TextOverflow.ellipsis),
                  ),
                  IconButton(
                    icon: const Icon(Icons.delete_outline, size: 18),
                    onPressed: () => _confirmDelete(context, ref),
                  ),
                ],
              ),
              const Spacer(),
              if (project.video != null)
                Text(
                  '${project.video!.width}x${project.video!.height} • ${project.video!.durationSec.toStringAsFixed(0)}s',
                  style: const TextStyle(color: Colors.white54, fontSize: 12),
                ),
              if (project.transcript != null)
                const Padding(
                  padding: EdgeInsets.only(top: 4),
                  child: Text('Transcribed', style: TextStyle(color: Colors.greenAccent, fontSize: 12)),
                ),
            ],
          ),
        ),
      ),
    );
  }

  Future<void> _confirmDelete(BuildContext context, WidgetRef ref) async {
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('Delete project?'),
        content: Text('"${project.name}" and all its files will be permanently deleted.'),
        actions: [
          TextButton(onPressed: () => Navigator.pop(context, false), child: const Text('Cancel')),
          FilledButton(onPressed: () => Navigator.pop(context, true), child: const Text('Delete')),
        ],
      ),
    );
    if (confirmed != true) return;
    final result = await ref.read(repositoryProvider).deleteProject(project.id);
    result.when(
      ok: (_) => ref.invalidate(projectsListProvider),
      err: (e) {
        if (context.mounted) {
          ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: ErrorView(error: e)));
        }
      },
    );
  }
}
