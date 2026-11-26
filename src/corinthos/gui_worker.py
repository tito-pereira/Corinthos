# SPDX-License-Identifier: MPL-2.0
"""One detached owner for an explicitly requested GUI operation."""

import sys

from corinthos.gui import _home, _id, perform
from corinthos.runtime import worker_ownership
from corinthos.runtime_gate import client_runtime


def main():
    identity = _id(sys.argv[1])
    with worker_ownership(_home(), identity), client_runtime():
        perform(identity)


if __name__ == "__main__":
    main()
