use std::io::{Error, ErrorKind};

pub fn is_runtime_shutdown(e: &Error) -> bool {
	if e.kind() == ErrorKind::Other
		&& e.to_string() == "A Tokio 1.x context was found, but it is being shutdown."
	{
		return true;
	}
	false
}
