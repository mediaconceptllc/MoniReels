import 'dart:async';

import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../data/repository.dart';
import '../domain/models.dart';
import 'providers.dart';

const _pollInterval = Duration(milliseconds: 500);
const _sseGraceperiod = Duration(seconds: 2);

/// Watches one job to completion: SSE first, automatically falling back to
/// polling /jobs/{id} every 500ms if the SSE stream errors or produces
/// nothing within a short grace period. Every export/preview/transcribe/
/// suggest job in the app goes through this single watcher.
Stream<Job> watchJob(Repository repo, String jobId) async* {
  final sseController = StreamController<Job>();
  late final StreamSubscription sseSub;

  sseSub = repo.jobEvents(jobId).listen(
    (job) {
      sseController.add(job);
      if (job.isTerminal) sseController.close();
    },
    onError: (Object _) => sseController.close(),
    onDone: sseController.close,
  );

  // Timeout resets on every event, so a healthy SSE stream never falls
  // through to polling — only a stalled/dropped connection does.
  final sseStream = sseController.stream.timeout(_sseGraceperiod, onTimeout: (sink) => sink.close());

  await for (final job in sseStream) {
    yield job;
    if (job.isTerminal) {
      await sseSub.cancel();
      return;
    }
  }
  await sseSub.cancel();

  // SSE stalled, errored, or closed early without a terminal state — poll instead.
  while (true) {
    final result = await repo.getJob(jobId);
    final job = result.when(ok: (j) => j, err: (_) => null);
    if (job == null) {
      await Future<void>.delayed(_pollInterval);
      continue;
    }
    yield job;
    if (job.isTerminal) return;
    await Future<void>.delayed(_pollInterval);
  }
}

/// autoDispose.family so leaving the page cancels the underlying watch.
final jobStreamProvider = StreamProvider.autoDispose.family<Job, String>((ref, jobId) {
  final repo = ref.watch(repositoryProvider);
  return watchJob(repo, jobId);
});
