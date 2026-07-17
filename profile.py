PROFILE = {
    "skill_weights": {
        # Top differentiators: datapath/networking core, his most marketable
        # assets (OVS customisation at Exatel, the DPU/networking roadmap).
        "ovs": 5,
        "open vswitch": 5,
        "dpdk": 5,
        "ebpf": 5,
        "xdp": 5,
        "linux kernel": 5,
        "yocto": 5,
        "network": 5,
        "networking": 5,
        # Solid systems/networking skills, one tier down.
        "embedded": 3,
        "firmware": 3,
        "rtos": 3,
        "netconf": 3,
        "opc ua": 3,
        "dds": 3,
        "grpc": 3,
        "protobuf": 2,
        "multithreading": 2,
        "lock-free": 2,
        "nats": 2,
        "llm": 3,
        # Generic stack: present on plenty of unrelated postings too, so it
        # barely moves the needle on its own.
        "c++": 3,
        "python": 1,
        "docker": 1,
        "cmake": 1,
        "jenkins": 1,
        "azure devops": 1,
        "git": 1,
        "pytest": 1,
        "robot framework": 1,
        "google test": 1,
    },
    # Hard-noes: weighted heavily enough that a hit drops the offer below
    # score_threshold even if it also has several positive skill hits.
    "negative_weights": {
        "java": -10,
        ".net": -10,
        "android": -10,
        "machine learning": -10,
        "tester": -10,
        "junior": -10,
        "banking": -8,
        "low latency": -6,
    },
    "target_employer_bonus": 4,
    "b2b_bonus": 2,
    "score_threshold": 1,
    # Companies explicitly named as near-term targets in Tomasz's roadmap.
    "target_employers": ["abb", "siemens", "aptiv", "continental", "harman"],
    "location": {
        "city_keywords": ["warszawa", "warsaw"],
        "remote_ok": True,
    },
    # Broad net used only to shrink justjoin.it/theprotocol.it's full active-offer
    # sitemaps (~10k and ~6k URLs respectively) down to a candidate set worth
    # fetching in full. Final relevance is decided by the scoring above, so
    # false positives here are fine.
    "slug_prefilter_keywords": [
        "embedded", "kernel", "firmware", "yocto", "dpdk", "ebpf", "xdp",
        "rtos", "automotive", "industrial", "fpga", "vhdl", "verilog",
        "plc", "scada", "linux",
    ],
    # nofluffjobs.com has its own topic taxonomy instead of free-text slugs,
    # so candidate discovery there browses these categories directly.
    "nofluffjobs_categories": ["embedded", "telecommunication", "electronics"],
}
