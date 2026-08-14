import 'dart:async';
import 'dart:convert';

import 'package:dio/dio.dart';

import 'errors.dart';
import 'result.dart';

/// Thin wrapper around Dio: every call returns a typed [Result] instead of
/// throwing, and the base URL can be changed at runtime (Settings page) since
/// the backend picks its port dynamically.
class ApiClient {
  ApiClient({required String baseUrl}) : _dio = Dio(BaseOptions(baseUrl: baseUrl, connectTimeout: const Duration(seconds: 5))) {
    // Long-running jobs are polled, not awaited directly, so response
    // timeouts stay generous rather than blocking the UI thread ever.
    _dio.options.receiveTimeout = const Duration(seconds: 30);
    _dio.options.sendTimeout = const Duration(seconds: 30);
  }

  final Dio _dio;
  String get baseUrl => _dio.options.baseUrl;

  void updateBaseUrl(String baseUrl) {
    _dio.options.baseUrl = baseUrl;
  }

  /// Set by providers.dart to re-run BackendLauncher.ensureRunning() and
  /// return the (possibly new) base URL, or null if it couldn't recover.
  /// Without this, a backend that dies mid-session (idle-shutdown, closing
  /// and reopening the app, a crash) leaves every subsequent request
  /// failing with "Could not reach the backend" forever - the only escape
  /// was fully restarting the app, losing the whole in-progress project.
  Future<String?> Function()? onConnectionLost;

  Future<bool>? _reconnecting;
  DateTime? _lastReconnectAttempt;

  // Spawning a whole new backend process is a heavy, drastic recovery step
  // - don't do it again within this long of the last attempt even if
  // connection errors keep coming (e.g. a still-starting process rejecting
  // connections for a moment). Without this floor, a burst of failures in
  // quick succession could each spawn their own duplicate backend, all
  // fighting over the same job/output files - exactly what happened before
  // this floor existed.
  static const _reconnectCooldown = Duration(seconds: 15);

  Future<bool> _tryReconnect() {
    if (_reconnecting != null) return _reconnecting!;
    final last = _lastReconnectAttempt;
    if (last != null && DateTime.now().difference(last) < _reconnectCooldown) {
      return Future.value(false);
    }
    _lastReconnectAttempt = DateTime.now();
    return _reconnecting = _reconnect().whenComplete(() => _reconnecting = null);
  }

  Future<bool> _reconnect() async {
    final callback = onConnectionLost;
    if (callback == null) return false;
    try {
      final newUrl = await callback();
      if (newUrl == null) return false;
      updateBaseUrl(newUrl);
      return true;
    } catch (_) {
      return false;
    }
  }

  // Only DioExceptionType.connectionError - a real "connection refused",
  // meaning nothing is listening on that port at all - indicates the
  // backend process itself is gone and worth the drastic step of killing
  // and respawning it. On localhost specifically, a dead process gives an
  // *instant* refused-connection error, never a slow one.
  //
  // connectionTimeout does NOT belong here, even though it sounds similar:
  // it means the OS accepted the connection attempt slowly, which on
  // localhost means the process IS alive but momentarily busy (e.g.
  // mid-Demucs-separation, GIL busy with heavy computation) and hasn't
  // gotten around to accept()-ing the new connection yet. Treating that as
  // "dead" was the actual bug: the 20s heartbeat's connection attempt would
  // time out while the backend was legitimately busy, get misclassified as
  // dead, and - because of the "kill the previous process before spawning"
  // safety net in BackendLauncher - kill the perfectly healthy, working
  // backend and replace it, interrupting whatever it was doing (once, this
  // fired mid-export). receive/send timeouts are excluded for the same
  // reason: the connection was established fine, the server is just slow.
  bool _indicatesDeadProcess(DioException e) => e.type == DioExceptionType.connectionError;

  Future<Result<dynamic>> get(String path, {Map<String, dynamic>? query}) =>
      _run(() => _dio.get(path, queryParameters: query), retryable: true);

  Future<Result<dynamic>> post(String path, {dynamic body}) =>
      // Never auto-retried: a lost response doesn't mean the request never
      // reached the server. Resubmitting a POST that starts a job (export,
      // transcribe, ...) can start a *second* one running concurrently
      // against the same output files - reconnecting still happens so the
      // connection is healed for the user's own manual retry.
      _run(() => _dio.post(path, data: body), retryable: false);

  Future<Result<dynamic>> put(String path, {dynamic body}) =>
      _run(() => _dio.put(path, data: body), retryable: true);

  Future<Result<dynamic>> delete(String path) => _run(() => _dio.delete(path), retryable: true);

  Future<Result<dynamic>> _run(Future<Response> Function() call, {required bool retryable}) async {
    try {
      final response = await call();
      return Ok(response.data);
    } on DioException catch (e) {
      if (_indicatesDeadProcess(e) && onConnectionLost != null) {
        final reconnected = await _tryReconnect();
        if (reconnected) {
          if (retryable) {
            try {
              final response = await call();
              return Ok(response.data);
            } on DioException catch (retryError) {
              return Err(_toAppError(retryError));
            }
          }
          // Not safe to auto-retry (a POST that might have already started
          // a job) - but the backend is back up now, so say so plainly
          // rather than the generic "is it running?" message, which reads
          // as a permanent failure when it's actually already fixed.
          return const Err(
            NetworkError.withMessage('The backend needed to restart and is ready now — please try again.'),
          );
        }
      }
      return Err(_toAppError(e));
    } catch (e) {
      return Err(UnknownError(e.toString()));
    }
  }

  AppError _toAppError(DioException e) {
    switch (e.type) {
      case DioExceptionType.connectionError:
      case DioExceptionType.connectionTimeout:
        return NetworkError(e.message);
      case DioExceptionType.receiveTimeout:
      case DioExceptionType.sendTimeout:
        return const NetworkError.withMessage('The backend took too long to respond.');
      default:
        break;
    }
    final status = e.response?.statusCode;
    if (status == 404) return NotFoundError(_detailOf(e));
    if (status == 422 || status == 400) {
      return ValidationError(_detailOf(e) ?? 'Invalid request.', e.response?.data?.toString());
    }
    if (status != null) return ServerError(status, _detailOf(e));
    return NetworkError(e.message);
  }

  String? _detailOf(DioException e) {
    final data = e.response?.data;
    if (data is Map && data['detail'] != null) return data['detail'].toString();
    return null;
  }

  /// SSE stream of job status objects. Falls back to polling (handled by the
  /// caller) if the stream errors out immediately.
  Stream<Map<String, dynamic>> jobEvents(String jobId) {
    final controller = StreamController<Map<String, dynamic>>();
    _dio
        .get<ResponseBody>(
      '/jobs/$jobId/events',
      options: Options(
        responseType: ResponseType.stream,
        headers: {'Accept': 'text/event-stream'},
        // No timeout: this stream stays open for as long as the job runs,
        // and a real job can go minutes between progress ticks (e.g. one
        // OpenAI suggestion call alone regularly takes 2-4 minutes with no
        // intermediate update) - inheriting the normal 30s receiveTimeout
        // here killed the connection mid-job even though the backend was
        // working fine, surfacing as a spurious "took too long to respond".
        receiveTimeout: Duration.zero,
        sendTimeout: Duration.zero,
      ),
    )
        .then((response) {
      final stream = response.data!.stream;
      final buffer = StringBuffer();
      stream.listen(
        (chunk) {
          buffer.write(utf8.decode(chunk, allowMalformed: true));
          final text = buffer.toString();
          final events = text.split('\n\n');
          if (events.length > 1) {
            buffer
              ..clear()
              ..write(events.last);
            for (final event in events.sublist(0, events.length - 1)) {
              final dataLine = event.split('\n').firstWhere(
                    (l) => l.startsWith('data:'),
                    orElse: () => '',
                  );
              if (dataLine.isEmpty) continue;
              try {
                final json = jsonDecode(dataLine.substring(5).trim()) as Map<String, dynamic>;
                controller.add(json);
              } catch (_) {
                // ignore malformed event
              }
            }
          }
        },
        onError: controller.addError,
        onDone: controller.close,
      );
    }).catchError((Object e) {
      controller.addError(e);
      controller.close();
    });
    return controller.stream;
  }
}
