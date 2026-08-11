import 'package:flutter/material.dart';

/// Dark theme only, per spec — no light-mode branch anywhere in the app.
ThemeData buildAppTheme() {
  final colorScheme = ColorScheme.fromSeed(
    seedColor: const Color(0xFF7C5CFF),
    brightness: Brightness.dark,
  );

  return ThemeData(
    useMaterial3: true,
    colorScheme: colorScheme,
    scaffoldBackgroundColor: const Color(0xFF121014),
    navigationRailTheme: NavigationRailThemeData(
      backgroundColor: const Color(0xFF17151B),
      selectedIconTheme: IconThemeData(color: colorScheme.primary),
      selectedLabelTextStyle: TextStyle(color: colorScheme.primary),
      unselectedLabelTextStyle: const TextStyle(color: Colors.white70),
    ),
    cardTheme: const CardThemeData(
      color: Color(0xFF1C1A21),
      elevation: 0,
      margin: EdgeInsets.zero,
    ),
    inputDecorationTheme: const InputDecorationTheme(
      filled: true,
      fillColor: Color(0xFF1C1A21),
      border: OutlineInputBorder(borderRadius: BorderRadius.all(Radius.circular(8))),
    ),
    dividerColor: Colors.white12,
  );
}
