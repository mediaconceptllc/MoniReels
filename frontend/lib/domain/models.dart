/// Hand-written models mirroring backend/app/models.dart's Pydantic models
/// exactly. No codegen (freezed/json_serializable) — see project rules.
library;

double _d(dynamic v) => (v as num).toDouble();
int _i(dynamic v) => (v as num).toInt();

class VideoMeta {
  VideoMeta({
    required this.path,
    required this.durationSec,
    required this.width,
    required this.height,
    required this.fps,
    required this.hasAudio,
    required this.codec,
    required this.thumbnailPath,
  });

  factory VideoMeta.fromJson(Map<String, dynamic> j) => VideoMeta(
        path: j['path'] as String,
        durationSec: _d(j['duration_sec']),
        width: _i(j['width']),
        height: _i(j['height']),
        fps: _d(j['fps']),
        hasAudio: j['has_audio'] as bool,
        codec: j['codec'] as String,
        thumbnailPath: j['thumbnail_path'] as String,
      );

  final String path;
  final double durationSec;
  final int width;
  final int height;
  final double fps;
  final bool hasAudio;
  final String codec;
  final String thumbnailPath;
}

class Word {
  Word({required this.start, required this.end, required this.text});

  factory Word.fromJson(Map<String, dynamic> j) =>
      Word(start: _d(j['start']), end: _d(j['end']), text: j['text'] as String);

  Map<String, dynamic> toJson() => {'start': start, 'end': end, 'text': text};

  final double start;
  final double end;
  final String text;
}

class Segment {
  Segment({
    required this.id,
    required this.start,
    required this.end,
    required this.text,
    this.speaker,
    this.words = const [],
  });

  factory Segment.fromJson(Map<String, dynamic> j) => Segment(
        id: j['id'] as String,
        start: _d(j['start']),
        end: _d(j['end']),
        text: j['text'] as String,
        speaker: j['speaker'] as String?,
        words: (j['words'] as List<dynamic>? ?? [])
            .map((w) => Word.fromJson(w as Map<String, dynamic>))
            .toList(),
      );

  Map<String, dynamic> toJson() => {
        'id': id,
        'start': start,
        'end': end,
        'text': text,
        'speaker': speaker,
        'words': words.map((w) => w.toJson()).toList(),
      };

  Segment copyWith({String? text}) =>
      Segment(id: id, start: start, end: end, text: text ?? this.text, speaker: speaker, words: words);

  final String id;
  final double start;
  final double end;
  final String text;
  final String? speaker;
  final List<Word> words;
}

class Transcript {
  Transcript({
    required this.language,
    required this.segments,
    required this.fullText,
    this.timingsEstimated = false,
  });

  factory Transcript.fromJson(Map<String, dynamic> j) => Transcript(
        language: j['language'] as String,
        segments: (j['segments'] as List<dynamic>)
            .map((s) => Segment.fromJson(s as Map<String, dynamic>))
            .toList(),
        fullText: j['full_text'] as String,
        timingsEstimated: j['timings_estimated'] as bool? ?? false,
      );

  Map<String, dynamic> toJson() => {
        'language': language,
        'segments': segments.map((s) => s.toJson()).toList(),
        'full_text': fullText,
        'timings_estimated': timingsEstimated,
      };

  Transcript copyWith({List<Segment>? segments}) => Transcript(
        language: language,
        segments: segments ?? this.segments,
        fullText: fullText,
        timingsEstimated: timingsEstimated,
      );

  final String language;
  final List<Segment> segments;
  final String fullText;
  final bool timingsEstimated;
}

class ShortIdea {
  ShortIdea({
    required this.id,
    required this.title,
    required this.hook,
    required this.description,
    required this.start,
    required this.end,
  });

  factory ShortIdea.fromJson(Map<String, dynamic> j) => ShortIdea(
        id: j['id'] as String,
        title: j['title'] as String,
        hook: j['hook'] as String,
        description: j['description'] as String,
        start: _d(j['start']),
        end: _d(j['end']),
      );

  final String id;
  final String title;
  final String hook;
  final String description;
  final double start;
  final double end;

  double get duration => end - start;
}

class KeepRange {
  KeepRange({required this.start, required this.end, required this.reason});

  factory KeepRange.fromJson(Map<String, dynamic> j) =>
      KeepRange(start: _d(j['start']), end: _d(j['end']), reason: j['reason'] as String);

  final double start;
  final double end;
  final String reason;
}

class YoutubePlan {
  YoutubePlan({required this.title, required this.description, required this.ranges, required this.totalDuration});

  factory YoutubePlan.fromJson(Map<String, dynamic> j) => YoutubePlan(
        title: j['title'] as String,
        description: j['description'] as String,
        ranges:
            (j['ranges'] as List<dynamic>).map((r) => KeepRange.fromJson(r as Map<String, dynamic>)).toList(),
        totalDuration: _d(j['total_duration']),
      );

  final String title;
  final String description;
  final List<KeepRange> ranges;
  final double totalDuration;
}

class Suggestions {
  Suggestions({required this.shorts, this.youtube});

  factory Suggestions.fromJson(Map<String, dynamic> j) => Suggestions(
        shorts:
            (j['shorts'] as List<dynamic>).map((s) => ShortIdea.fromJson(s as Map<String, dynamic>)).toList(),
        youtube: j['youtube'] == null ? null : YoutubePlan.fromJson(j['youtube'] as Map<String, dynamic>),
      );

  final List<ShortIdea> shorts;
  final YoutubePlan? youtube;
}

class Clip {
  Clip({
    required this.id,
    required this.sourcePath,
    required this.start,
    required this.end,
    required this.order,
    this.transitionIn,
  });

  factory Clip.fromJson(Map<String, dynamic> j) => Clip(
        id: j['id'] as String,
        sourcePath: j['source_path'] as String,
        start: _d(j['start']),
        end: _d(j['end']),
        order: _i(j['order']),
        transitionIn: j['transition_in'] as String?,
      );

  Map<String, dynamic> toJson() => {
        'id': id,
        'source_path': sourcePath,
        'start': start,
        'end': end,
        'order': order,
        'transition_in': transitionIn,
      };

  Clip copyWith({double? start, double? end, int? order, String? transitionIn}) => Clip(
        id: id,
        sourcePath: sourcePath,
        start: start ?? this.start,
        end: end ?? this.end,
        order: order ?? this.order,
        transitionIn: transitionIn ?? this.transitionIn,
      );

  final String id;
  final String sourcePath;
  final double start;
  final double end;
  final int order;
  final String? transitionIn;

  double get duration => end - start;
}

class TransitionSetting {
  TransitionSetting({this.type = 'Cross Fade', this.duration = 0.5});

  factory TransitionSetting.fromJson(Map<String, dynamic> j) =>
      TransitionSetting(type: j['type'] as String? ?? 'Cross Fade', duration: _d(j['duration'] ?? 0.5));

  Map<String, dynamic> toJson() => {'type': type, 'duration': duration};

  TransitionSetting copyWith({String? type, double? duration}) =>
      TransitionSetting(type: type ?? this.type, duration: duration ?? this.duration);

  final String type;
  final double duration;
}

class SubtitleStyle {
  SubtitleStyle({
    this.enabled = true,
    this.fontFamily = 'Arial',
    this.fontSize = 42,
    this.primaryColor = '#FFFFFF',
    this.outlineColor = '#000000',
    this.outlineWidth = 2.0,
    this.shadow = 0.0,
    this.position = 'bottom',
    this.marginV = 40,
  });

  factory SubtitleStyle.fromJson(Map<String, dynamic> j) => SubtitleStyle(
        enabled: j['enabled'] as bool? ?? true,
        fontFamily: j['font_family'] as String? ?? 'Arial',
        fontSize: _i(j['font_size'] ?? 42),
        primaryColor: j['primary_color'] as String? ?? '#FFFFFF',
        outlineColor: j['outline_color'] as String? ?? '#000000',
        outlineWidth: _d(j['outline_width'] ?? 2.0),
        shadow: _d(j['shadow'] ?? 0.0),
        position: j['position'] as String? ?? 'bottom',
        marginV: _i(j['margin_v'] ?? 40),
      );

  Map<String, dynamic> toJson() => {
        'enabled': enabled,
        'font_family': fontFamily,
        'font_size': fontSize,
        'primary_color': primaryColor,
        'outline_color': outlineColor,
        'outline_width': outlineWidth,
        'shadow': shadow,
        'position': position,
        'margin_v': marginV,
      };

  SubtitleStyle copyWith({
    bool? enabled,
    String? fontFamily,
    int? fontSize,
    String? primaryColor,
    String? outlineColor,
    double? outlineWidth,
    double? shadow,
    String? position,
    int? marginV,
  }) =>
      SubtitleStyle(
        enabled: enabled ?? this.enabled,
        fontFamily: fontFamily ?? this.fontFamily,
        fontSize: fontSize ?? this.fontSize,
        primaryColor: primaryColor ?? this.primaryColor,
        outlineColor: outlineColor ?? this.outlineColor,
        outlineWidth: outlineWidth ?? this.outlineWidth,
        shadow: shadow ?? this.shadow,
        position: position ?? this.position,
        marginV: marginV ?? this.marginV,
      );

  final bool enabled;
  final String fontFamily;
  final int fontSize;
  final String primaryColor;
  final String outlineColor;
  final double outlineWidth;
  final double shadow;
  final String position;
  final int marginV;
}

class ExportSettings {
  ExportSettings({
    this.container = 'mp4',
    this.orientation = 'landscape',
    this.portraitFill = 'blur',
    this.useHwaccel = true,
    this.crf = 20,
    this.preset = 'medium',
    this.burnSubtitles = false,
    this.writeSrt = true,
  });

  factory ExportSettings.fromJson(Map<String, dynamic> j) => ExportSettings(
        container: j['container'] as String? ?? 'mp4',
        orientation: j['orientation'] as String? ?? 'landscape',
        portraitFill: j['portrait_fill'] as String? ?? 'blur',
        useHwaccel: j['use_hwaccel'] as bool? ?? true,
        crf: _i(j['crf'] ?? 20),
        preset: j['preset'] as String? ?? 'medium',
        burnSubtitles: j['burn_subtitles'] as bool? ?? false,
        writeSrt: j['write_srt'] as bool? ?? true,
      );

  Map<String, dynamic> toJson() => {
        'container': container,
        'orientation': orientation,
        'portrait_fill': portraitFill,
        'use_hwaccel': useHwaccel,
        'crf': crf,
        'preset': preset,
        'burn_subtitles': burnSubtitles,
        'write_srt': writeSrt,
      };

  ExportSettings copyWith({
    String? container,
    String? orientation,
    String? portraitFill,
    bool? useHwaccel,
    int? crf,
    String? preset,
    bool? burnSubtitles,
    bool? writeSrt,
  }) =>
      ExportSettings(
        container: container ?? this.container,
        orientation: orientation ?? this.orientation,
        portraitFill: portraitFill ?? this.portraitFill,
        useHwaccel: useHwaccel ?? this.useHwaccel,
        crf: crf ?? this.crf,
        preset: preset ?? this.preset,
        burnSubtitles: burnSubtitles ?? this.burnSubtitles,
        writeSrt: writeSrt ?? this.writeSrt,
      );

  final String container;
  final String orientation;
  final String portraitFill;
  final bool useHwaccel;
  final int crf;
  final String preset;
  final bool burnSubtitles;
  final bool writeSrt;
}

class Project {
  Project({
    required this.id,
    required this.name,
    required this.createdAt,
    required this.updatedAt,
    this.schemaVersion = 1,
    this.video,
    this.transcript,
    this.suggestions,
    this.clips = const [],
    TransitionSetting? transition,
    SubtitleStyle? subtitleStyle,
    ExportSettings? export,
  })  : transition = transition ?? TransitionSetting(),
        subtitleStyle = subtitleStyle ?? SubtitleStyle(),
        export = export ?? ExportSettings();

  factory Project.fromJson(Map<String, dynamic> j) => Project(
        id: j['id'] as String,
        name: j['name'] as String,
        createdAt: _d(j['created_at']),
        updatedAt: _d(j['updated_at']),
        schemaVersion: _i(j['schema_version'] ?? 1),
        video: j['video'] == null ? null : VideoMeta.fromJson(j['video'] as Map<String, dynamic>),
        transcript:
            j['transcript'] == null ? null : Transcript.fromJson(j['transcript'] as Map<String, dynamic>),
        suggestions:
            j['suggestions'] == null ? null : Suggestions.fromJson(j['suggestions'] as Map<String, dynamic>),
        clips: (j['clips'] as List<dynamic>? ?? []).map((c) => Clip.fromJson(c as Map<String, dynamic>)).toList(),
        transition: j['transition'] == null
            ? null
            : TransitionSetting.fromJson(j['transition'] as Map<String, dynamic>),
        subtitleStyle: j['subtitle_style'] == null
            ? null
            : SubtitleStyle.fromJson(j['subtitle_style'] as Map<String, dynamic>),
        export: j['export'] == null ? null : ExportSettings.fromJson(j['export'] as Map<String, dynamic>),
      );

  Map<String, dynamic> toJson() => {
        'schema_version': schemaVersion,
        'id': id,
        'name': name,
        'created_at': createdAt,
        'updated_at': updatedAt,
        'video': video == null
            ? null
            : {
                'path': video!.path,
                'duration_sec': video!.durationSec,
                'width': video!.width,
                'height': video!.height,
                'fps': video!.fps,
                'has_audio': video!.hasAudio,
                'codec': video!.codec,
                'thumbnail_path': video!.thumbnailPath,
              },
        'transcript': transcript?.toJson(),
        'suggestions': suggestions == null
            ? null
            : {
                'shorts': suggestions!.shorts
                    .map((s) => {
                          'id': s.id,
                          'title': s.title,
                          'hook': s.hook,
                          'description': s.description,
                          'start': s.start,
                          'end': s.end,
                        })
                    .toList(),
                'youtube': suggestions!.youtube == null
                    ? null
                    : {
                        'title': suggestions!.youtube!.title,
                        'description': suggestions!.youtube!.description,
                        'ranges': suggestions!.youtube!.ranges
                            .map((r) => {'start': r.start, 'end': r.end, 'reason': r.reason})
                            .toList(),
                        'total_duration': suggestions!.youtube!.totalDuration,
                      },
              },
        'clips': clips.map((c) => c.toJson()).toList(),
        'transition': transition.toJson(),
        'subtitle_style': subtitleStyle.toJson(),
        'export': export.toJson(),
      };

  Project copyWith({
    String? name,
    VideoMeta? video,
    Transcript? transcript,
    Suggestions? suggestions,
    List<Clip>? clips,
    TransitionSetting? transition,
    SubtitleStyle? subtitleStyle,
    ExportSettings? export,
  }) =>
      Project(
        id: id,
        name: name ?? this.name,
        createdAt: createdAt,
        updatedAt: updatedAt,
        schemaVersion: schemaVersion,
        video: video ?? this.video,
        transcript: transcript ?? this.transcript,
        suggestions: suggestions ?? this.suggestions,
        clips: clips ?? this.clips,
        transition: transition ?? this.transition,
        subtitleStyle: subtitleStyle ?? this.subtitleStyle,
        export: export ?? this.export,
      );

  final int schemaVersion;
  final String id;
  final String name;
  final double createdAt;
  final double updatedAt;
  final VideoMeta? video;
  final Transcript? transcript;
  final Suggestions? suggestions;
  final List<Clip> clips;
  final TransitionSetting transition;
  final SubtitleStyle subtitleStyle;
  final ExportSettings export;
}

enum JobState { queued, running, done, failed, canceled }

JobState _jobStateFromString(String s) => switch (s) {
      'queued' => JobState.queued,
      'running' => JobState.running,
      'done' => JobState.done,
      'failed' => JobState.failed,
      'canceled' => JobState.canceled,
      _ => JobState.failed,
    };

class Job {
  Job({
    required this.jobId,
    required this.state,
    required this.progress,
    required this.stage,
    required this.message,
    this.result,
    this.error,
  });

  factory Job.fromJson(Map<String, dynamic> j) => Job(
        jobId: j['job_id'] as String,
        state: _jobStateFromString(j['state'] as String),
        progress: _d(j['progress'] ?? 0),
        stage: j['stage'] as String? ?? '',
        message: j['message'] as String? ?? '',
        result: j['result'] as Map<String, dynamic>?,
        error: j['error'] as String?,
      );

  final String jobId;
  final JobState state;
  final double progress;
  final String stage;
  final String message;
  final Map<String, dynamic>? result;
  final String? error;

  bool get isTerminal => state == JobState.done || state == JobState.failed || state == JobState.canceled;
}

class Capabilities {
  Capabilities({
    required this.ffmpegAvailable,
    required this.ffmpegVersion,
    required this.xfadeTransitions,
    required this.encodersListed,
    this.workingHwaccelEncoder,
    required this.fonts,
  });

  factory Capabilities.fromJson(Map<String, dynamic> j) => Capabilities(
        ffmpegAvailable: j['ffmpeg_available'] as bool,
        ffmpegVersion: j['ffmpeg_version'] as String? ?? '',
        xfadeTransitions: (j['xfade_transitions'] as List<dynamic>).cast<String>(),
        encodersListed: (j['encoders_listed'] as List<dynamic>).cast<String>(),
        workingHwaccelEncoder: j['working_hwaccel_encoder'] as String?,
        fonts: (j['fonts'] as List<dynamic>).cast<String>(),
      );

  final bool ffmpegAvailable;
  final String ffmpegVersion;
  final List<String> xfadeTransitions;
  final List<String> encodersListed;
  final String? workingHwaccelEncoder;
  final List<String> fonts;
}

/// Secrets are never sent back by the backend — only whether they're set.
class BackendSettings {
  BackendSettings({
    required this.chimegeSttUrl,
    required this.chimegeTokenSet,
    required this.chimegeMaxAudioSec,
    required this.openaiModel,
    required this.openaiBaseUrl,
    required this.openaiApiKeySet,
  });

  factory BackendSettings.fromJson(Map<String, dynamic> j) => BackendSettings(
        chimegeSttUrl: j['chimege_stt_url'] as String,
        chimegeTokenSet: j['chimege_token_set'] as bool,
        chimegeMaxAudioSec: _i(j['chimege_max_audio_sec']),
        openaiModel: j['openai_model'] as String,
        openaiBaseUrl: j['openai_base_url'] as String,
        openaiApiKeySet: j['openai_api_key_set'] as bool,
      );

  final String chimegeSttUrl;
  final bool chimegeTokenSet;
  final int chimegeMaxAudioSec;
  final String openaiModel;
  final String openaiBaseUrl;
  final bool openaiApiKeySet;
}
