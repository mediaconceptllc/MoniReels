import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../application/providers.dart';
import '../settings_page.dart';
import 'step1_import.dart';
import 'step2_settings.dart';
import 'step3_export.dart';

enum _WizardStep { importVideo, settings, export }

const _stepLabels = ['1. Import & Analyze', '2. Style', '3. Export'];

/// The whole app, top to bottom: pick a video (everything from project
/// creation through AI suggestions happens automatically), pick subtitle +
/// transition settings, export. Replaces the old 10-page nav rail.
class WizardPage extends ConsumerStatefulWidget {
  const WizardPage({super.key});

  @override
  ConsumerState<WizardPage> createState() => _WizardPageState();
}

class _WizardPageState extends ConsumerState<WizardPage> {
  _WizardStep _step = _WizardStep.importVideo;

  void _goTo(_WizardStep step) => setState(() => _step = step);

  void _startOver() {
    ref.read(currentProjectProvider.notifier).clear();
    setState(() => _step = _WizardStep.importVideo);
  }

  /// Explicitly ends both processes in order — backend first, then this
  /// one — rather than just closing the window. A plain window close still
  /// only triggers AppLifecycleListener.onExitRequested (bootstrap.dart),
  /// which does the same killIfSpawned() but relies on the OS delivering
  /// that exit-requested signal; this button guarantees the same order
  /// unconditionally, and actually terminates this process (exit(0)) once
  /// the backend is gone, rather than leaving it to the window manager.
  Future<void> _quit() async {
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('Quit autoReel?'),
        content: const Text(
          'This stops the backend and closes the app. Any unsaved progress in the current project will be lost.',
        ),
        actions: [
          TextButton(onPressed: () => Navigator.of(context).pop(false), child: const Text('Cancel')),
          FilledButton(
            onPressed: () => Navigator.of(context).pop(true),
            style: FilledButton.styleFrom(backgroundColor: Colors.redAccent),
            child: const Text('Quit'),
          ),
        ],
      ),
    );
    if (confirmed != true) return;

    ref.read(backendLauncherProvider).killIfSpawned();
    exit(0);
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: SafeArea(
        child: Column(
          children: [
            _StepHeader(current: _step, onStartOver: _startOver, onQuit: _quit),
            const Divider(height: 1),
            Expanded(
              child: switch (_step) {
                _WizardStep.importVideo => Step1Import(onNext: () => _goTo(_WizardStep.settings)),
                _WizardStep.settings => Step2Settings(
                    onNext: () => _goTo(_WizardStep.export),
                    onBack: () => _goTo(_WizardStep.importVideo),
                  ),
                _WizardStep.export => Step3Export(onBack: () => _goTo(_WizardStep.settings)),
              },
            ),
          ],
        ),
      ),
    );
  }
}

void _openSettings(BuildContext context) {
  Navigator.of(context).push(
    MaterialPageRoute(
      builder: (_) => Scaffold(
        appBar: AppBar(title: const Text('Settings')),
        body: const SettingsPage(),
      ),
    ),
  );
}

class _StepHeader extends StatelessWidget {
  const _StepHeader({required this.current, required this.onStartOver, required this.onQuit});
  final _WizardStep current;
  final VoidCallback onStartOver;
  final VoidCallback onQuit;

  @override
  Widget build(BuildContext context) {
    final index = _WizardStep.values.indexOf(current);
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 24, vertical: 16),
      child: Row(
        children: [
          Image.asset('assets/icon_mark.png', width: 28, height: 28),
          const SizedBox(width: 16),
          Expanded(
            child: SingleChildScrollView(
              scrollDirection: Axis.horizontal,
              child: Row(
                mainAxisSize: MainAxisSize.min,
                children: [
                  for (var i = 0; i < _stepLabels.length; i++) ...[
                    if (i > 0)
                      const Padding(
                        padding: EdgeInsets.symmetric(horizontal: 12),
                        child: Icon(Icons.chevron_right, color: Colors.white24),
                      ),
                    Text(
                      _stepLabels[i],
                      style: Theme.of(context).textTheme.titleMedium?.copyWith(
                            color: i == index ? Theme.of(context).colorScheme.primary : Colors.white38,
                            fontWeight: i == index ? FontWeight.bold : FontWeight.normal,
                          ),
                    ),
                  ],
                ],
              ),
            ),
          ),
          IconButton(
            onPressed: () => _openSettings(context),
            icon: const Icon(Icons.settings_outlined),
            tooltip: 'Settings',
          ),
          TextButton.icon(
            onPressed: onStartOver,
            icon: const Icon(Icons.refresh),
            label: const Text('Start over'),
          ),
          const SizedBox(width: 8),
          IconButton(
            onPressed: onQuit,
            icon: const Icon(Icons.power_settings_new),
            tooltip: 'Quit (stops the backend too)',
          ),
        ],
      ),
    );
  }
}
