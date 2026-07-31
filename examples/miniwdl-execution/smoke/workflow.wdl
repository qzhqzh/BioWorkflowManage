version 1.0

task write_probe {
  command <<<
    set -euo pipefail
    printf '%s\n' 'miniwdl-container-ok' > probe.txt
  >>>

  output {
    File probe = "probe.txt"
    String status = read_string("probe.txt")
  }

  runtime {
    docker: "python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de"
    cpu: 1
    memory: "256 MiB"
  }
}

workflow miniwdl_runtime_smoke {
  call write_probe

  output {
    File probe = write_probe.probe
    String status = write_probe.status
  }
}
