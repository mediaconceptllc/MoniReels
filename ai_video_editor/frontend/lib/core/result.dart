import 'errors.dart';

/// A typed success/failure wrapper so repositories never throw raw
/// exceptions into UI code — every backend call returns a Result.
sealed class Result<T> {
  const Result();

  bool get isOk => this is Ok<T>;

  R when<R>({
    required R Function(T value) ok,
    required R Function(AppError error) err,
  }) {
    final self = this;
    if (self is Ok<T>) return ok(self.value);
    return err((self as Err<T>).error);
  }
}

class Ok<T> extends Result<T> {
  const Ok(this.value);
  final T value;
}

class Err<T> extends Result<T> {
  const Err(this.error);
  final AppError error;
}
