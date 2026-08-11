import 'dart:async';

import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../core/errors.dart';
import '../data/repository.dart';
import '../domain/models.dart';

const _autosaveDebounce = Duration(seconds: 2);

class ProjectState {
  const ProjectState({this.project, this.isDirty = false, this.isSaving = false, this.loadError});

  final Project? project;
  final bool isDirty;
  final bool isSaving;
  final AppError? loadError;

  ProjectState copyWith({
    Project? project,
    bool? isDirty,
    bool? isSaving,
    AppError? loadError,
    bool clearError = false,
  }) =>
      ProjectState(
        project: project ?? this.project,
        isDirty: isDirty ?? this.isDirty,
        isSaving: isSaving ?? this.isSaving,
        loadError: clearError ? null : (loadError ?? this.loadError),
      );
}

/// Holds the currently open project. Every page reads from this; any update
/// marks the project dirty and autosaves 2s after the last change, per spec §9.
class ProjectController extends StateNotifier<ProjectState> {
  ProjectController(this._repo) : super(const ProjectState());

  final Repository _repo;
  Timer? _debounce;

  Future<void> load(String projectId) async {
    state = state.copyWith(clearError: true);
    final result = await _repo.getProject(projectId);
    result.when(
      ok: (project) => state = ProjectState(project: project),
      err: (e) => state = state.copyWith(loadError: e),
    );
  }

  void setProject(Project project) {
    state = ProjectState(project: project);
  }

  void clear() {
    _debounce?.cancel();
    state = const ProjectState();
  }

  /// Applies `updater` to the current project, marks dirty, and (re)starts
  /// the 2s autosave timer.
  void update(Project Function(Project current) updater) {
    final current = state.project;
    if (current == null) return;
    state = state.copyWith(project: updater(current), isDirty: true);
    _debounce?.cancel();
    _debounce = Timer(_autosaveDebounce, _flush);
  }

  /// Saves immediately, bypassing the debounce — call before starting a job
  /// that reads the project from disk on the backend (transcribe/suggest/export).
  Future<void> saveNow() async {
    _debounce?.cancel();
    await _flush();
  }

  Future<void> _flush() async {
    final project = state.project;
    if (project == null || !state.isDirty) return;
    state = state.copyWith(isSaving: true);
    final result = await _repo.saveProject(project);
    result.when(
      ok: (saved) => state = state.copyWith(project: saved, isDirty: false, isSaving: false),
      err: (e) => state = state.copyWith(isSaving: false, loadError: e),
    );
  }

  @override
  void dispose() {
    _debounce?.cancel();
    super.dispose();
  }
}
