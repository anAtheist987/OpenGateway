// Modified by Tsinghua University, 2026
// Original source: https://github.com/agentgateway/agentgateway
// Licensed under the Apache License, Version 2.0

fn main() {
	#[cfg(all(not(test), not(feature = "internal_benches")))]
	panic!("benches must have -F internal_benches");
	use agentgateway as _;
	divan::main();
}
