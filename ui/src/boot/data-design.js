// Hook: design target data (owner: T-P4-02b; see boot/README.md).
// The static tier / mode metadata (data/static.js), then the design target's synthetic data
// (design/data.js, built on the same metadata), then the design target's demo controls
// (design/demo.js, T-P4-07: the overview's 层级回退 control, B-22's prototype advice cards, the
// palette's demo block and the QA hooks, registered at T.ext / T.cmd before the pages render).
import '../data/static.js';
import '../design/data.js';
import '../design/demo.js';
