import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../application/providers.dart';
import '../../domain/models.dart';
import '../shell.dart';

class DashboardPage extends ConsumerWidget {
  const DashboardPage({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final health = ref.watch(healthProvider);
    final projects = ref.watch(projectsListProvider);

    return Padding(
      padding: const EdgeInsets.all(24),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text('Dashboard', style: Theme.of(context).textTheme.headlineSmall),
          const SizedBox(height: 16),
          _HealthCard(health: health),
          const SizedBox(height: 24),
          Text('Recent Projects', style: Theme.of(context).textTheme.titleMedium),
          const SizedBox(height: 12),
          Expanded(
            child: projects.when(
              data: (list) => list.isEmpty
                  ? const Text('No projects yet. Import a video to get started.', style: TextStyle(color: Colors.white54))
                  : ListView.separated(
                      itemCount: list.length,
                      separatorBuilder: (_, _) => const SizedBox(height: 8),
                      itemBuilder: (context, i) => _ProjectTile(project: list[i]),
                    ),
              loading: () => const Center(child: CircularProgressIndicator()),
              error: (e, _) => Text('Could not load projects: $e', style: const TextStyle(color: Colors.redAccent)),
            ),
          ),
        ],
      ),
    );
  }
}

class _HealthCard extends ConsumerWidget {
  const _HealthCard({required this.health});
  final AsyncValue<Map<String, dynamic>> health;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: health.when(
          data: (data) {
            final ffmpegOk = data['ffmpeg'] == true;
            return Row(
              children: [
                Icon(Icons.circle, size: 12, color: Colors.greenAccent.shade400),
                const SizedBox(width: 8),
                const Text('Backend: connected'),
                const SizedBox(width: 24),
                Icon(
                  ffmpegOk ? Icons.check_circle : Icons.error,
                  size: 16,
                  color: ffmpegOk ? Colors.greenAccent.shade400 : Colors.redAccent,
                ),
                const SizedBox(width: 8),
                Text(ffmpegOk ? 'FFmpeg ${data['version']}' : 'FFmpeg not found'),
              ],
            );
          },
          loading: () => const Row(children: [
            SizedBox(width: 16, height: 16, child: CircularProgressIndicator(strokeWidth: 2)),
            SizedBox(width: 12),
            Text('Checking backend...'),
          ]),
          error: (e, _) => Row(
            children: [
              const Icon(Icons.circle, size: 12, color: Colors.redAccent),
              const SizedBox(width: 8),
              const Text('Backend: unreachable'),
              const Spacer(),
              TextButton(
                onPressed: () => ref.invalidate(healthProvider),
                child: const Text('Retry'),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

class _ProjectTile extends ConsumerWidget {
  const _ProjectTile({required this.project});
  final Project project;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    return Card(
      child: ListTile(
        leading: const Icon(Icons.movie_outlined),
        title: Text(project.name),
        subtitle: Text(project.video == null ? 'Importing...' : '${project.video!.width}x${project.video!.height} • ${project.video!.durationSec.toStringAsFixed(0)}s'),
        onTap: () {
          ref.read(currentProjectProvider.notifier).setProject(project);
          ref.read(selectedNavIndexProvider.notifier).state = 2;
        },
      ),
    );
  }
}
