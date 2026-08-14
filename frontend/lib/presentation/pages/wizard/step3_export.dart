import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../application/providers.dart';
import '../../../core/errors.dart';
import '../../widgets/error_view.dart';
import '../../widgets/job_progress.dart';

/// No settings here on purpose — everything needed was already chosen in
/// Step 2. One button renders a standalone file per suggested idea (up to 3
/// reels + up to 3 youtube compilations).
class Step3Export extends ConsumerStatefulWidget {
  const Step3Export({super.key, required this.onBack});
  final VoidCallback onBack;

  @override
  ConsumerState<Step3Export> createState() => _Step3ExportState();
}

class _Step3ExportState extends ConsumerState<Step3Export> {
  String? _jobId;
  List<Map<String, dynamic>>? _outputs;
  String? _error;
  // Covers the gap between clicking Export and JobProgressView actually
  // appearing (project save + the export request itself - can take a
  // while if the backend needs reconnecting first) - without this the
  // button just sits there doing nothing visible, which reads as stuck.
  bool _starting = false;

  Future<void> _startExport(String projectId) async {
    setState(() {
      _error = null;
      _outputs = null;
      _starting = true;
    });
    await ref.read(currentProjectProvider.notifier).saveNow();
    final result = await ref.read(repositoryProvider).exportAll(projectId);
    result.when(
      ok: (jobId) => setState(() {
        _jobId = jobId;
        _starting = false;
      }),
      err: (e) => setState(() {
        _error = e.message;
        _starting = false;
      }),
    );
  }

  void _openFolder(String outputPath) {
    final dir = File(outputPath).parent.path;
    Process.run('explorer', [dir]);
  }

  @override
  Widget build(BuildContext context) {
    final project = ref.watch(currentProjectProvider).project;
    if (project == null) {
      return const Center(child: Text('No project loaded.', style: TextStyle(color: Colors.white54)));
    }
    final hasIdeas = project.suggestions != null &&
        (project.suggestions!.shorts.isNotEmpty || project.suggestions!.youtube.isNotEmpty);

    return Padding(
      padding: const EdgeInsets.all(24),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text('Export', style: Theme.of(context).textTheme.headlineSmall),
          const SizedBox(height: 8),
          const Text(
            'Renders one file per suggested idea, with the subtitle and transition settings from the previous step.',
            style: TextStyle(color: Colors.white54),
          ),
          const SizedBox(height: 24),
          Expanded(
            child: SingleChildScrollView(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  FilledButton.icon(
                    onPressed: !hasIdeas || _jobId != null || _starting ? null : () => _startExport(project.id),
                    icon: _starting
                        ? const SizedBox(
                            width: 16,
                            height: 16,
                            child: CircularProgressIndicator(strokeWidth: 2, color: Colors.white70),
                          )
                        : const Icon(Icons.ios_share),
                    label: Text(_starting ? 'Starting export...' : 'Export'),
                  ),
                  if (!hasIdeas)
                    const Padding(
                      padding: EdgeInsets.only(top: 8),
                      child: Text('No AI suggestions to export yet.', style: TextStyle(color: Colors.white54)),
                    ),
                  if (_error != null)
                    Padding(
                      padding: const EdgeInsets.only(top: 12),
                      child: ErrorView(error: UnknownError(_error)),
                    ),
                  if (_jobId != null && _outputs == null)
                    Padding(
                      padding: const EdgeInsets.symmetric(vertical: 16),
                      child: JobProgressView(
                        jobId: _jobId!,
                        onDone: (result) => setState(() {
                          final raw = result?['outputs'] as List<dynamic>? ?? [];
                          _outputs = raw.cast<Map<String, dynamic>>();
                        }),
                        onFailed: (e) => setState(() => _error = e),
                      ),
                    ),
                  if (_outputs != null) ...[
                    Padding(
                      padding: const EdgeInsets.symmetric(vertical: 16),
                      child: Row(
                        children: [
                          const Icon(Icons.check_circle, color: Colors.greenAccent),
                          const SizedBox(width: 8),
                          Text('${_outputs!.length} files exported', style: Theme.of(context).textTheme.titleMedium),
                          const Spacer(),
                          if (_outputs!.isNotEmpty)
                            TextButton.icon(
                              onPressed: () => _openFolder(_outputs!.first['output_path'] as String),
                              icon: const Icon(Icons.folder_open),
                              label: const Text('Open output folder'),
                            ),
                        ],
                      ),
                    ),
                    for (final o in _outputs!)
                      Card(
                        child: ListTile(
                          leading: Icon(o['kind'] == 'reel' ? Icons.smartphone : Icons.smart_display_outlined),
                          title: Text(o['title'] as String),
                          subtitle: Text(o['output_path'] as String),
                        ),
                      ),
                  ],
                ],
              ),
            ),
          ),
          const SizedBox(height: 16),
          Align(
            alignment: Alignment.centerLeft,
            child: OutlinedButton(onPressed: widget.onBack, child: const Text('Back')),
          ),
        ],
      ),
    );
  }
}
