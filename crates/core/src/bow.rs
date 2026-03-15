// Modified by Tsinghua University, 2026
// Original source: https://github.com/agentgateway/agentgateway
// Licensed under the Apache License, Version 2.0

// OwnedOrBorrowed is exactly like Cow but doesn't require the type to be Clone
pub enum OwnedOrBorrowed<'a, T> {
	Borrowed(&'a T),
	Owned(T),
}

impl<'a, T> std::ops::Deref for OwnedOrBorrowed<'a, T> {
	type Target = T;

	fn deref(&self) -> &T {
		match self {
			Self::Borrowed(v) => v,
			Self::Owned(v) => v,
		}
	}
}

impl<'a, T> std::convert::AsRef<T> for OwnedOrBorrowed<'a, T> {
	fn as_ref(&self) -> &T {
		self
	}
}
