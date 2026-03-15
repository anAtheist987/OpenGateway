#[derive(Debug)]
pub(crate) enum Msg {
	Line(Vec<u8>),
	Shutdown,
}
