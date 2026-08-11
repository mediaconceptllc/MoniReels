import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'pages/dashboard_page.dart';
import 'pages/export_page.dart';
import 'pages/import_page.dart';
import 'pages/projects_page.dart';
import 'pages/settings_page.dart';
import 'pages/subtitle_page.dart';
import 'pages/suggestions_page.dart';
import 'pages/timeline_page.dart';
import 'pages/transcript_page.dart';
import 'pages/transitions_page.dart';

final selectedNavIndexProvider = StateProvider<int>((ref) => 0);

const _destinations = [
  (icon: Icons.dashboard_outlined, selectedIcon: Icons.dashboard, label: 'Dashboard'),
  (icon: Icons.folder_outlined, selectedIcon: Icons.folder, label: 'Projects'),
  (icon: Icons.file_upload_outlined, selectedIcon: Icons.file_upload, label: 'Import'),
  (icon: Icons.subject_outlined, selectedIcon: Icons.subject, label: 'Transcript'),
  (icon: Icons.auto_awesome_outlined, selectedIcon: Icons.auto_awesome, label: 'AI Suggestions'),
  (icon: Icons.view_timeline_outlined, selectedIcon: Icons.view_timeline, label: 'Timeline'),
  (icon: Icons.compare_arrows_outlined, selectedIcon: Icons.compare_arrows, label: 'Transitions'),
  (icon: Icons.closed_caption_outlined, selectedIcon: Icons.closed_caption, label: 'Subtitle'),
  (icon: Icons.ios_share_outlined, selectedIcon: Icons.ios_share, label: 'Export'),
  (icon: Icons.settings_outlined, selectedIcon: Icons.settings, label: 'Settings'),
];

final _pages = <Widget>[
  const DashboardPage(),
  const ProjectsPage(),
  const ImportPage(),
  const TranscriptPage(),
  const SuggestionsPage(),
  const TimelinePage(),
  const TransitionsPage(),
  const SubtitlePage(),
  const ExportPage(),
  const SettingsPage(),
];

class AppShell extends ConsumerWidget {
  const AppShell({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final selected = ref.watch(selectedNavIndexProvider);

    return Scaffold(
      body: Row(
        children: [
          // Scrollable so 10 destinations never overflow on a short window.
          SingleChildScrollView(
            child: ConstrainedBox(
              constraints: BoxConstraints(minHeight: MediaQuery.of(context).size.height),
              child: IntrinsicHeight(
                child: NavigationRail(
                  selectedIndex: selected,
                  onDestinationSelected: (i) => ref.read(selectedNavIndexProvider.notifier).state = i,
                  labelType: NavigationRailLabelType.all,
                  leading: const Padding(
                    padding: EdgeInsets.symmetric(vertical: 12),
                    child: Icon(Icons.movie_creation_outlined, size: 28),
                  ),
                  destinations: [
                    for (final d in _destinations)
                      NavigationRailDestination(
                          icon: Icon(d.icon), selectedIcon: Icon(d.selectedIcon), label: Text(d.label)),
                  ],
                ),
              ),
            ),
          ),
          const VerticalDivider(width: 1),
          Expanded(child: IndexedStack(index: selected, children: _pages)),
        ],
      ),
    );
  }
}
