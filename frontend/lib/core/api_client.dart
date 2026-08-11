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

  Future<Result<dynamic>> get(String path, {Map<String, dynamic>? query}) =>
      _run(() => _dio.get(path, queryParameters: query));

  Future<Result<dynamic>> post(String path, {dynamic body}) =>
      _run(() => _dio.post(path, data: body));

  Future<Result<dynamic>> put(String path, {dynamic body}) =>
      _run(() => _dio.put(path, data: body));

  Future<Result<dynamic>> delete(String path) => _run(() => _dio.delete(path));

  Future<Result<dynamic>> _run(Future<Response> Function() call) async {
    try {
      final response = await call();
      return Ok(response.data);
    } on DioException catch (e) {
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
        return NetworkError('The backend took too long to respond.');
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
      options: Options(responseType: ResponseType.stream, headers: {'Accept': 'text/event-stream'}),
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
