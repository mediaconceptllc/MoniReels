import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../application/job_watcher.dart';
import '../../application/providers.dart';
import '../../core/errors.dart';
import '../../domain/models.dart';
import 'error_view.dart';

/// Progress bar + stage label + Cancel, wired to a single job via the
/// SSE/polling watcher. Calls [onDone]/[onFailed] exactly once when the job
/// reaches a terminal state.
class JobProgressView extends ConsumerStatefulWidget {
  const JobProgressView({
    super.key,
    required this.jobId,
    this.onDone,
    this.onFailed,
    this.onCanceled,
  });

  final String jobId;
  final void Function(Map<String, dynamic>? result)? onDone;
  final void Function(String error)? onFailed;
  final VoidCallback? onCanceled;

  @override
  ConsumerState<JobProgressView> createState() => _JobProgressViewState();
}

class _JobProgressViewState extends ConsumerState<JobProgressView> {
  bool _notified = false;

  @override
  Widget build(BuildContext context) {
    final asyncJob = ref.watch(jobStreamProvider(widget.jobId));

    return asyncJob.when(
      data: (job) {
        _notifyIfTerminal(job);
        return _buildProgress(context, job);
      },
      loading: () => const LinearProgressIndicator(),
      error: (e, _) => ErrorView(error: NetworkError(e.toString())),
    );
  }

  void _notifyIfTerminal(Job job) {
    if (_notified || !job.isTerminal) return;
    _notified = true;
    WidgetsBinding.instance.addPostFrameCallback((_) {
      switch (job.state) {
        case JobState.done:
          widget.onDone?.call(job.result);
        case JobState.failed:
          widget.onFailed?.call(job.error ?? 'Job failed');
        case JobState.canceled:
          widget.onCanceled?.call();
        default:
          break;
      }
    });
  }

  Widget _buildProgress(BuildContext context, Job job) {
    final canCancel = job.state == JobState.queued || job.state == JobState.running;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        ClipRRect(
          borderRadius: BorderRadius.circular(4),
          child: LinearProgressIndicator(value: job.progress.clamp(0, 1), minHeight: 8),
        ),
        const SizedBox(height: 8),
        Row(
          children: [
            Expanded(
              child: Text(
                job.message.isEmpty ? job.stage : '${job.stage}: ${job.message}',
                style: Theme.of(context).textTheme.bodySmall,
                overflow: TextOverflow.ellipsis,
              ),
            ),
            Text('${(job.progress * 100).toStringAsFixed(0)}%'),
            if (canCancel) ...[
              const SizedBox(width: 8),
              TextButton(
                onPressed: () => ref.read(repositoryProvider).cancelJob(widget.jobId),
                child: const Text('Cancel'),
              ),
            ],
          ],
        ),
      ],
    );
  }
}
