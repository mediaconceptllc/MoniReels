import '../core/api_client.dart';
import '../core/result.dart';
import '../domain/models.dart';

/// The only place that talks to the FastAPI backend. Every page/controller
/// goes through this — never calls Dio/HTTP directly.
class Repository {
  Repository(this._client);

  final ApiClient _client;
  ApiClient get client => _client;

  Future<Result<Map<String, dynamic>>> health() async {
    final r = await _client.get('/health');
    return r.when(ok: (v) => Ok(v as Map<String, dynamic>), err: (e) => Err(e));
  }

  Future<Result<Capabilities>> capabilities() async {
    final r = await _client.get('/capabilities');
    return r.when(
      ok: (v) => Ok(Capabilities.fromJson(v as Map<String, dynamic>)),
      err: (e) => Err(e),
    );
  }

  Future<Result<({String projectId, String jobId})>> createProject(String videoPath, {String? name}) async {
    final r = await _client.post('/projects', body: {'video_path': videoPath, 'name': name});
    return r.when(
      ok: (v) {
        final m = v as Map<String, dynamic>;
        return Ok((projectId: m['project_id'] as String, jobId: m['job_id'] as String));
      },
      err: (e) => Err(e),
    );
  }

  Future<Result<List<Project>>> listProjects() async {
    final r = await _client.get('/projects');
    return r.when(
      ok: (v) => Ok((v as List<dynamic>).map((p) => Project.fromJson(p as Map<String, dynamic>)).toList()),
      err: (e) => Err(e),
    );
  }

  Future<Result<Project>> getProject(String id) async {
    final r = await _client.get('/projects/$id');
    return r.when(ok: (v) => Ok(Project.fromJson(v as Map<String, dynamic>)), err: (e) => Err(e));
  }

  Future<Result<Project>> saveProject(Project project) async {
    final r = await _client.put('/projects/${project.id}', body: project.toJson());
    return r.when(ok: (v) => Ok(Project.fromJson(v as Map<String, dynamic>)), err: (e) => Err(e));
  }

  Future<Result<void>> deleteProject(String id) async {
    final r = await _client.delete('/projects/$id');
    return r.when(ok: (_) => const Ok(null), err: (e) => Err(e));
  }

  Future<Result<String>> transcribe(String projectId) => _startJob('/projects/$projectId/transcribe');
  Future<Result<String>> suggest(String projectId) => _startJob('/projects/$projectId/suggest');
  Future<Result<String>> preview(String projectId) => _startJob('/projects/$projectId/preview');
  Future<Result<String>> export(String projectId) => _startJob('/projects/$projectId/export');

  Future<Result<String>> _startJob(String path) async {
    final r = await _client.post(path);
    return r.when(ok: (v) => Ok((v as Map<String, dynamic>)['job_id'] as String), err: (e) => Err(e));
  }

  Future<Result<Job>> getJob(String jobId) async {
    final r = await _client.get('/jobs/$jobId');
    return r.when(ok: (v) => Ok(Job.fromJson(v as Map<String, dynamic>)), err: (e) => Err(e));
  }

  Future<Result<void>> cancelJob(String jobId) async {
    final r = await _client.post('/jobs/$jobId/cancel');
    return r.when(ok: (_) => const Ok(null), err: (e) => Err(e));
  }

  Stream<Job> jobEvents(String jobId) => _client.jobEvents(jobId).map(Job.fromJson);

  /// URL to fetch a backend-produced file (thumbnail, preview, export) via
  /// GET /files?path=... — pass straight to Image.network / media_kit.
  String fileUrl(String path) => '${_client.baseUrl}/files?path=${Uri.encodeQueryComponent(path)}';
}
