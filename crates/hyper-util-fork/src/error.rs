// Copyright 2026 Tsinghua University
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// This file was created by Tsinghua University and is not part of
// the original agentgateway project by Solo.io.

// use std::error::Error;
//
// pub(crate) fn find<'a, E: Error + 'static>(top: &'a (dyn Error + 'static)) -> Option<&'a E> {
// let mut err = Some(top);
// while let Some(src) = err {
// if src.is::<E>() {
// return src.downcast_ref();
// }
// err = src.source();
// }
// None
// }
