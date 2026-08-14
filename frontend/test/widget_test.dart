import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:ai_video_editor_frontend/application/backend_launcher.dart';
import 'package:ai_video_editor_frontend/application/providers.dart';
import 'package:ai_video_editor_frontend/domain/models.dart';
import 'package:ai_video_editor_frontend/main.dart';

/// Skips real process spawning/networking — just echoes back the dev URL
/// as if a backend were already running there.
class _FakeBackendLauncher extends BackendLauncher {
  @override
  Future<Uri> ensureRunning({required Uri devUrl}) async => devUrl;

  @override
  void killIfSpawned() {}
}

void main() {
  testWidgets('Wizard opens on Step 1, ready for a video to be picked', (WidgetTester tester) async {
    // Override the backend-hitting providers so this test never opens a real
    // socket (Dio's IO client doesn't respect flutter_test's fake clock).
    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          backendLauncherProvider.overrideWithValue(_FakeBackendLauncher()),
          healthProvider.overrideWith((ref) async => {'status': 'ok', 'ffmpeg': true, 'version': 'test'}),
          capabilitiesProvider.overrideWith(
            (ref) async => Capabilities(
              ffmpegAvailable: true,
              ffmpegVersion: 'test',
              xfadeTransitions: const [],
              encodersListed: const [],
              fonts: const [],
            ),
          ),
          projectsListProvider.overrideWith((ref) async => <Project>[]),
          backendSettingsProvider.overrideWith(
            (ref) async => BackendSettings(
              chimegeSttUrl: 'https://api.chimege.com/v1.2',
              chimegeTokenSet: false,
              chimegeMaxAudioSec: 60,
              openaiModel: '',
              openaiBaseUrl: 'https://api.openai.com/v1',
              openaiApiKeySet: false,
            ),
          ),
        ],
        child: const AiVideoEditorApp(),
      ),
    );
    // Two pumps: AppBootstrap now waits for its own _launch() (the fake
    // launcher resolving) before it even starts watching healthProvider,
    // so getting from the initial loading screen to WizardPage takes two
    // sequential async resolutions rather than one.
    await tester.pump();
    await tester.pump();

    expect(find.text('1. Import & Analyze'), findsOneWidget);
    expect(find.text('Pick a video to get started'), findsOneWidget);
    expect(find.text('Choose video file...'), findsOneWidget);
    expect(
      find.byWidgetPredicate(
        (w) => w is Image && w.image is AssetImage && (w.image as AssetImage).assetName == 'assets/icon_mark.png',
      ),
      findsOneWidget,
    );
  });
}
