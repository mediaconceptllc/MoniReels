import 'dart:async';

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
  DateTime? _runStartedAt;
  Timer? _elapsedTicker;
  int _elapsedSeconds = 0;

  @override
  void dispose() {
    _elapsedTicker?.cancel();
    super.dispose();
  }

  /// A long-running stage (e.g. CPU-only Demucs separation) can go many
  /// seconds — sometimes minutes — between progress updates from the
  /// backend. A static LinearProgressIndicator sitting at a fixed % with no
  /// motion in that gap reads as "stuck", even though real work is still
  /// happening. This keeps a per-second-ticking elapsed timer running for
  /// the whole time the job is active, purely so *something* visibly moves
  /// regardless of whether the numeric progress itself has changed yet.
  void _ensureElapsedTicking(Job job) {
    final running = job.state == JobState.queued || job.state == JobState.running;
    if (!running) {
      _elapsedTicker?.cancel();
      _elapsedTicker = null;
      _runStartedAt = null;
      return;
    }
    _runStartedAt ??= DateTime.now();
    _elapsedTicker ??= Timer.periodic(const Duration(seconds: 1), (_) {
      if (!mounted) return;
      setState(() => _elapsedSeconds = DateTime.now().difference(_runStartedAt!).inSeconds);
    });
  }

  @override
  Widget build(BuildContext context) {
    final asyncJob = ref.watch(jobStreamProvider(widget.jobId));

    return asyncJob.when(
      data: (job) {
        _notifyIfTerminal(job);
        _ensureElapsedTicking(job);
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

  String _formatElapsed(int seconds) {
    final m = seconds ~/ 60;
    final s = seconds % 60;
    return m > 0 ? '${m}m ${s}s' : '${s}s';
  }

  Widget _buildProgress(BuildContext context, Job job) {
    final canCancel = job.state == JobState.queued || job.state == JobState.running;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Stack(
          children: [
            ClipRRect(
              borderRadius: BorderRadius.circular(4),
              child: LinearProgressIndicator(value: job.progress.clamp(0, 1), minHeight: 8),
            ),
            // A second, indeterminate bar layered on top - it never stops
            // animating on its own, unlike the determinate bar above, so
            // there's always visible motion even while the backend is deep
            // in a stage (e.g. Demucs separation) with no progress ticks to
            // report for a stretch.
            if (canCancel)
              ClipRRect(
                borderRadius: BorderRadius.circular(4),
                child: SizedBox(
                  height: 8,
                  child: LinearProgressIndicator(
                    value: null,
                    minHeight: 8,
                    backgroundColor: Colors.transparent,
                    valueColor: AlwaysStoppedAnimation(
                      Theme.of(context).colorScheme.primary.withValues(alpha: 0.25),
                    ),
                  ),
                ),
              ),
          ],
        ),
        const SizedBox(height: 8),
        Row(
          children: [
            if (canCancel) ...[
              const SizedBox(
                width: 12,
                height: 12,
                child: CircularProgressIndicator(strokeWidth: 2),
              ),
              const SizedBox(width: 8),
            ],
            Expanded(
              child: Text(
                job.message.isEmpty ? job.stage : '${job.stage}: ${job.message}',
                style: Theme.of(context).textTheme.bodySmall,
                overflow: TextOverflow.ellipsis,
              ),
            ),
            if (canCancel) ...[
              Text(
                'running ${_formatElapsed(_elapsedSeconds)}',
                style: Theme.of(context).textTheme.bodySmall?.copyWith(color: Colors.white54),
              ),
              const SizedBox(width: 8),
            ],
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
