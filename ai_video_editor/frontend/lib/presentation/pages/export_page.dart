import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../application/providers.dart';
import '../../core/errors.dart';
import '../../domain/models.dart';
import '../widgets/error_view.dart';
import '../widgets/job_progress.dart';

class ExportPage extends ConsumerStatefulWidget {
  const ExportPage({super.key});

  @override
  ConsumerState<ExportPage> createState() => _ExportPageState();
}

class _ExportPageState extends ConsumerState<ExportPage> {
  String? _jobId;
  String? _outputPath;
  String? _error;

  Future<void> _startExport(String projectId) async {
    setState(() {
      _error = null;
      _outputPath = null;
    });
    await ref.read(currentProjectProvider.notifier).saveNow();
    final result = await ref.read(repositoryProvider).export(projectId);
    result.when(
      ok: (jobId) => setState(() => _jobId = jobId),
      err: (e) => setState(() => _error = e.message),
    );
  }

  void _openFolder(String outputPath) {
    final dir = File(outputPath).parent.path;
    Process.run('explorer', [dir]);
  }

  @override
  Widget build(BuildContext context) {
    final projectState = ref.watch(currentProjectProvider);
    final project = projectState.project;
    if (project == null) {
      return const Center(child: Text('No project loaded.', style: TextStyle(color: Colors.white54)));
    }
    final export = project.export;

    void update(ExportSettings Function(ExportSettings) f) {
      ref.read(currentProjectProvider.notifier).update((p) => p.copyWith(export: f(p.export)));
    }

    return Padding(
      padding: const EdgeInsets.all(24),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text('Export', style: Theme.of(context).textTheme.headlineSmall),
          const SizedBox(height: 16),
          Expanded(
            child: SingleChildScrollView(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Row(
                    children: [
                      Expanded(
                        child: DropdownButtonFormField<String>(
                          initialValue: export.container,
                          decoration: const InputDecoration(labelText: 'Container'),
                          items: const [
                            DropdownMenuItem(value: 'mp4', child: Text('MP4')),
                            DropdownMenuItem(value: 'mov', child: Text('MOV')),
                          ],
                          onChanged: (v) => v == null ? null : update((e) => e.copyWith(container: v)),
                        ),
                      ),
                      const SizedBox(width: 16),
                      Expanded(
                        child: DropdownButtonFormField<String>(
                          initialValue: export.orientation,
                          decoration: const InputDecoration(labelText: 'Orientation'),
                          items: const [
                            DropdownMenuItem(value: 'landscape', child: Text('Landscape (1920x1080)')),
                            DropdownMenuItem(value: 'portrait', child: Text('Portrait (1080x1920)')),
                          ],
                          onChanged: (v) => v == null ? null : update((e) => e.copyWith(orientation: v)),
                        ),
                      ),
                    ],
                  ),
                  if (export.orientation == 'portrait') ...[
                    const SizedBox(height: 16),
                    DropdownButtonFormField<String>(
                      initialValue: export.portraitFill,
                      decoration: const InputDecoration(labelText: 'Portrait fill'),
                      items: const [
                        DropdownMenuItem(value: 'blur', child: Text('Blurred background')),
                        DropdownMenuItem(value: 'crop', child: Text('Crop to fill')),
                        DropdownMenuItem(value: 'pad', child: Text('Pad with black bars')),
                      ],
                      onChanged: (v) => v == null ? null : update((e) => e.copyWith(portraitFill: v)),
                    ),
                  ],
                  const SizedBox(height: 16),
                  SwitchListTile(
                    title: const Text('Use hardware acceleration'),
                    value: export.useHwaccel,
                    onChanged: (v) => update((e) => e.copyWith(useHwaccel: v)),
                  ),
                  Row(
                    children: [
                      const Text('Quality (CRF)'),
                      Expanded(
                        child: Slider(
                          value: export.crf.toDouble(),
                          min: 15,
                          max: 30,
                          divisions: 15,
                          label: '${export.crf}',
                          onChanged: (v) => update((e) => e.copyWith(crf: v.round())),
                        ),
                      ),
                      SizedBox(width: 32, child: Text('${export.crf}')),
                    ],
                  ),
                  DropdownButtonFormField<String>(
                    initialValue: export.preset,
                    decoration: const InputDecoration(labelText: 'Encoder preset'),
                    items: const [
                      DropdownMenuItem(value: 'ultrafast', child: Text('Ultrafast')),
                      DropdownMenuItem(value: 'veryfast', child: Text('Very fast')),
                      DropdownMenuItem(value: 'fast', child: Text('Fast')),
                      DropdownMenuItem(value: 'medium', child: Text('Medium')),
                      DropdownMenuItem(value: 'slow', child: Text('Slow')),
                    ],
                    onChanged: (v) => v == null ? null : update((e) => e.copyWith(preset: v)),
                  ),
                  const SizedBox(height: 24),
                  FilledButton.icon(
                    onPressed: project.clips.isEmpty ? null : () => _startExport(project.id),
                    icon: const Icon(Icons.ios_share),
                    label: const Text('Start Export'),
                  ),
                  if (project.clips.isEmpty)
                    const Padding(
                      padding: EdgeInsets.only(top: 8),
                      child: Text('Add clips on the Timeline page first.', style: TextStyle(color: Colors.white54)),
                    ),
                  if (_error != null)
                    Padding(
                      padding: const EdgeInsets.only(top: 12),
                      child: ErrorView(error: UnknownError(_error)),
                    ),
                  if (_jobId != null)
                    Padding(
                      padding: const EdgeInsets.symmetric(vertical: 16),
                      child: JobProgressView(
                        jobId: _jobId!,
                        onDone: (result) => setState(() => _outputPath = result?['output_path'] as String?),
                        onFailed: (e) => setState(() => _error = e),
                      ),
                    ),
                  if (_outputPath != null)
                    Card(
                      child: ListTile(
                        leading: const Icon(Icons.check_circle, color: Colors.greenAccent),
                        title: const Text('Export complete'),
                        subtitle: Text(_outputPath!),
                        trailing: TextButton(
                          onPressed: () => _openFolder(_outputPath!),
                          child: const Text('Open folder'),
                        ),
                      ),
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
