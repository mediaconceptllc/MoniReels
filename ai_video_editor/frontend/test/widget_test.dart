import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:ai_video_editor_frontend/application/providers.dart';
import 'package:ai_video_editor_frontend/domain/models.dart';
import 'package:ai_video_editor_frontend/main.dart';

void main() {
  testWidgets('App shell renders the nav rail with all pages', (WidgetTester tester) async {
    // Override the backend-hitting providers so this test never opens a real
    // socket (Dio's IO client doesn't respect flutter_test's fake clock).
    await tester.pumpWidget(
      ProviderScope(
        overrides: [
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
        ],
        child: const AiVideoEditorApp(),
      ),
    );
    await tester.pump();

    expect(find.text('Dashboard'), findsWidgets);
    expect(find.text('Settings'), findsWidgets);
    expect(find.byIcon(Icons.movie_creation_outlined), findsOneWidget);
  });
}
