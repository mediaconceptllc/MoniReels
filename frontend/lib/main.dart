import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:media_kit/media_kit.dart';

import 'core/theme.dart';
import 'presentation/bootstrap.dart';

void main() {
  MediaKit.ensureInitialized();
  runApp(const ProviderScope(child: AiVideoEditorApp()));
}

class AiVideoEditorApp extends StatelessWidget {
  const AiVideoEditorApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'AI Video Editor',
      debugShowCheckedModeBanner: false,
      themeMode: ThemeMode.dark,
      darkTheme: buildAppTheme(),
      theme: buildAppTheme(),
      home: const AppBootstrap(),
    );
  }
}
