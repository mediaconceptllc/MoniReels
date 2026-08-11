import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../core/api_client.dart';
import '../data/repository.dart';
import '../domain/models.dart';
import 'backend_launcher.dart';
import 'project_controller.dart';

const kDefaultBackendUrl = 'http://127.0.0.1:8000';

/// Overridden in widget tests with a fake that skips real process spawning
/// and networking.
final backendLauncherProvider = Provider<BackendLauncher>((ref) => BackendLauncher());

/// Mutable so Settings can repoint at a different backend without restarting
/// the app — the backend's actual port is only known once it's running
/// (see spec §10), so this defaults to the common dev value.
final apiClientProvider = Provider<ApiClient>((ref) => ApiClient(baseUrl: kDefaultBackendUrl));

final repositoryProvider = Provider<Repository>((ref) => Repository(ref.watch(apiClientProvider)));

/// Bumped whenever the backend URL changes, to force health/capabilities to re-fetch.
final backendGenerationProvider = StateProvider<int>((ref) => 0);

final healthProvider = FutureProvider.autoDispose<Map<String, dynamic>>((ref) async {
  ref.watch(backendGenerationProvider);
  final result = await ref.watch(repositoryProvider).health();
  return result.when(ok: (v) => v, err: (e) => throw e);
});

final capabilitiesProvider = FutureProvider.autoDispose<Capabilities>((ref) async {
  ref.watch(backendGenerationProvider);
  final result = await ref.watch(repositoryProvider).capabilities();
  return result.when(ok: (v) => v, err: (e) => throw e);
});

final projectsListProvider = FutureProvider.autoDispose<List<Project>>((ref) async {
  final result = await ref.watch(repositoryProvider).listProjects();
  return result.when(ok: (v) => v, err: (e) => throw e);
});

/// Bumped after a settings save so the Settings page's "currently set" status refreshes.
final settingsGenerationProvider = StateProvider<int>((ref) => 0);

final backendSettingsProvider = FutureProvider.autoDispose<BackendSettings>((ref) async {
  ref.watch(settingsGenerationProvider);
  ref.watch(backendGenerationProvider);
  final result = await ref.watch(repositoryProvider).getSettings();
  return result.when(ok: (v) => v, err: (e) => throw e);
});

/// The single project every page reads from, per spec §9.
final currentProjectProvider = StateNotifierProvider<ProjectController, ProjectState>((ref) {
  return ProjectController(ref.watch(repositoryProvider));
});
