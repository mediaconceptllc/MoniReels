import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../application/providers.dart';
import '../../domain/models.dart';
import '../shell.dart';

class _QuickAction {
  const _QuickAction(this.title, this.subtitle, this.icon, this.gradient, this.navIndex);
  final String title;
  final String subtitle;
  final IconData icon;
  final List<Color> gradient;
  final int navIndex;
}

const _heroActions = [
  _QuickAction(
    'Import Video',
    'Bring in a new source clip',
    Icons.file_upload_outlined,
    [Color(0xFF6C4CFF), Color(0xFFEB3B94)],
    2,
  ),
  _QuickAction(
    'AI Suggestions',
    'Auto-generate shorts & highlights',
    Icons.auto_awesome_outlined,
    [Color(0xFFEB3B94), Color(0xFFFF8A3D)],
    4,
  ),
  _QuickAction(
    'Export',
    'Render your final video',
    Icons.ios_share_outlined,
    [Color(0xFF2FB8E0), Color(0xFF6C4CFF)],
    8,
  ),
];

const _toolActions = [
  _QuickAction('Projects', 'Browse saved projects', Icons.folder_outlined, [Color(0xFFEB3B94), Color(0xFFEB3B94)], 1),
  _QuickAction('Transcript', 'Read & edit the transcript', Icons.subject_outlined, [Color(0xFF6C4CFF), Color(0xFF6C4CFF)], 3),
  _QuickAction('Timeline', 'Arrange & trim clips', Icons.view_timeline_outlined, [Color(0xFFFF8A3D), Color(0xFFFF8A3D)], 5),
  _QuickAction('Transitions', 'Pick join effects', Icons.compare_arrows_outlined, [Color(0xFF2FB8E0), Color(0xFF2FB8E0)], 6),
  _QuickAction('Subtitle', 'Style burned-in captions', Icons.closed_caption_outlined, [Color(0xFF3DDC97), Color(0xFF3DDC97)], 7),
  _QuickAction('Settings', 'Backend, credentials, logs', Icons.settings_outlined, [Colors.white54, Colors.white54], 9),
];

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
          Row(
            children: [
              const Image(image: AssetImage('assets/icon_mark.png'), width: 36, height: 36),
              const SizedBox(width: 12),
              Text('autoReel', style: Theme.of(context).textTheme.headlineSmall),
            ],
          ),
          const SizedBox(height: 16),
          _HealthCard(health: health),
          const SizedBox(height: 24),
          Expanded(
            child: SingleChildScrollView(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text('Quick actions', style: Theme.of(context).textTheme.titleMedium),
                  const SizedBox(height: 12),
                  LayoutBuilder(
                    builder: (context, constraints) {
                      final cardWidth = (constraints.maxWidth - 24) / 3;
                      return Wrap(
                        spacing: 12,
                        runSpacing: 12,
                        children: [
                          for (final a in _heroActions)
                            SizedBox(
                              width: cardWidth.clamp(220, 420),
                              child: _HeroActionCard(action: a),
                            ),
                        ],
                      );
                    },
                  ),
                  const SizedBox(height: 28),
                  Text('More tools', style: Theme.of(context).textTheme.titleMedium),
                  const SizedBox(height: 12),
                  GridView.builder(
                    shrinkWrap: true,
                    physics: const NeverScrollableScrollPhysics(),
                    gridDelegate: const SliverGridDelegateWithMaxCrossAxisExtent(
                      maxCrossAxisExtent: 220,
                      mainAxisExtent: 136,
                      crossAxisSpacing: 12,
                      mainAxisSpacing: 12,
                    ),
                    itemCount: _toolActions.length,
                    itemBuilder: (context, i) => _ToolActionCard(action: _toolActions[i]),
                  ),
                  const SizedBox(height: 28),
                  Text('Recent Projects', style: Theme.of(context).textTheme.titleMedium),
                  const SizedBox(height: 12),
                  projects.when(
                    data: (list) => list.isEmpty
                        ? const Text('No projects yet. Import a video to get started.',
                            style: TextStyle(color: Colors.white54))
                        : Column(
                            children: [for (final p in list) _ProjectTile(project: p)],
                          ),
                    loading: () => const Center(child: CircularProgressIndicator()),
                    error: (e, _) =>
                        Text('Could not load projects: $e', style: const TextStyle(color: Colors.redAccent)),
                  ),
                ],
              ),
            ),
          ),
        ],
      ),
    );
  }
}

class _HeroActionCard extends ConsumerWidget {
  const _HeroActionCard({required this.action});
  final _QuickAction action;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    return Material(
      color: Colors.transparent,
      borderRadius: BorderRadius.circular(16),
      child: InkWell(
        borderRadius: BorderRadius.circular(16),
        onTap: () => ref.read(selectedNavIndexProvider.notifier).state = action.navIndex,
        child: Container(
          constraints: const BoxConstraints(minHeight: 140),
          padding: const EdgeInsets.all(18),
          decoration: BoxDecoration(
            borderRadius: BorderRadius.circular(16),
            gradient: LinearGradient(
              colors: action.gradient,
              begin: Alignment.topLeft,
              end: Alignment.bottomRight,
            ),
          ),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            mainAxisSize: MainAxisSize.min,
            children: [
              Icon(action.icon, color: Colors.white, size: 28),
              const SizedBox(height: 16),
              Text(
                action.title,
                style: const TextStyle(color: Colors.white, fontWeight: FontWeight.bold, fontSize: 18),
              ),
              const SizedBox(height: 4),
              Text(
                action.subtitle,
                maxLines: 2,
                overflow: TextOverflow.ellipsis,
                style: TextStyle(color: Colors.white.withValues(alpha: 0.85), fontSize: 12),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

class _ToolActionCard extends ConsumerWidget {
  const _ToolActionCard({required this.action});
  final _QuickAction action;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    return Card(
      child: InkWell(
        onTap: () => ref.read(selectedNavIndexProvider.notifier).state = action.navIndex,
        child: Padding(
          padding: const EdgeInsets.all(14),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Container(
                width: 34,
                height: 34,
                decoration: BoxDecoration(
                  color: action.gradient.first.withValues(alpha: 0.18),
                  borderRadius: BorderRadius.circular(8),
                ),
                child: Icon(action.icon, size: 18, color: action.gradient.first),
              ),
              const Spacer(),
              Text(
                action.title,
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                style: const TextStyle(fontWeight: FontWeight.w600),
              ),
              const SizedBox(height: 2),
              Text(
                action.subtitle,
                style: const TextStyle(color: Colors.white54, fontSize: 11),
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
              ),
            ],
          ),
        ),
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
      margin: const EdgeInsets.only(bottom: 8),
      child: ListTile(
        leading: const Icon(Icons.movie_outlined),
        title: Text(project.name),
        subtitle: Text(project.video == null
            ? 'Importing...'
            : '${project.video!.width}x${project.video!.height} • ${project.video!.durationSec.toStringAsFixed(0)}s'),
        onTap: () {
          ref.read(currentProjectProvider.notifier).setProject(project);
          ref.read(selectedNavIndexProvider.notifier).state = 2;
        },
      ),
    );
  }
}
