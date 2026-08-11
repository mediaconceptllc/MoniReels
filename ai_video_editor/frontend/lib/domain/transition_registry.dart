/// Mirrors backend/app/transition/registry.py's REGISTRY table so the
/// Transitions page can show every UI option and grey out ones the
/// installed FFmpeg doesn't support (per /capabilities' xfade_transitions).
library;

class TransitionOption {
  const TransitionOption(this.uiName, this.xfadeName, this.fallbackXfadeName);

  final String uiName;
  final String xfadeName;
  final String? fallbackXfadeName;
}

const List<TransitionOption> kTransitionRegistry = [
  TransitionOption('Fade', 'fadeblack', 'fade'),
  TransitionOption('Cross Fade', 'fade', null),
  TransitionOption('Dissolve', 'dissolve', 'fade'),
  TransitionOption('Wipe Left', 'wipeleft', 'fade'),
  TransitionOption('Wipe Right', 'wiperight', 'fade'),
  TransitionOption('Slide Left', 'slideleft', 'wipeleft'),
  TransitionOption('Slide Right', 'slideright', 'wiperight'),
  TransitionOption('Zoom', 'zoomin', 'fade'),
  TransitionOption('Circle Open', 'circleopen', 'fade'),
  TransitionOption('Circle Close', 'circleclose', 'fade'),
  TransitionOption('Blur', 'hblur', 'dissolve'),
  TransitionOption('Pixelize', 'pixelize', 'dissolve'),
];

bool isTransitionSupported(TransitionOption option, List<String> supportedXfade) {
  if (supportedXfade.contains(option.xfadeName)) return true;
  if (option.fallbackXfadeName != null && supportedXfade.contains(option.fallbackXfadeName)) return true;
  return false;
}

const double kMinTransitionDuration = 0.25;
const double kMaxTransitionDuration = 2.0;
const double kTransitionDurationStep = 0.05;
