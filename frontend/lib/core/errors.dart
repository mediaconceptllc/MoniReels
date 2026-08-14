/// Typed, human-readable errors. UI code should always render `message`
/// rather than a raw exception/stack trace.
sealed class AppError {
  const AppError(this.message, this.details);

  final String message;
  final String? details;

  String get copyText => details == null ? message : '$message\n\n$details';
}

class NetworkError extends AppError {
  const NetworkError([String? details]) : super('Could not reach the backend. Is it running?', details);

  /// For cases with a more specific, more actionable message than the
  /// generic default above (e.g. "took too long to respond", "needed to
  /// restart, try again") - the default constructor has no way to override
  /// `message` (it's always the generic text), which silently swallowed a
  /// couple of intended custom messages elsewhere in this codebase.
  const NetworkError.withMessage(super.message, [super.details]);
}

class ServerError extends AppError {
  const ServerError(this.statusCode, [String? details]) : super('Backend returned an error (HTTP $statusCode).', details);

  final int statusCode;
}

class ValidationError extends AppError {
  const ValidationError(super.message, [super.details]);
}

class NotFoundError extends AppError {
  const NotFoundError([String? details]) : super('Not found.', details);
}

class UnknownError extends AppError {
  const UnknownError([String? details]) : super('Something went wrong.', details);
}
