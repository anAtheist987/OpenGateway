// We don't control the codegen, so disable any code warnings in the
// proto modules.
#[allow(warnings)]
#[allow(clippy::derive_partial_eq_without_eq)]
pub mod service {
	pub mod discovery {
		pub mod v3 {
			tonic::include_proto!("envoy.service.discovery.v3");
		}
	}
}
