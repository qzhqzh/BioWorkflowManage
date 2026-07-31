version 1.0

task fastp_runtime {
  input {
    File reads_1
    File reads_2
    Int threads = 2
  }

  command <<<
    set -euo pipefail
    mkdir -p outputs
    fastp \
      --in1 ~{reads_1} \
      --in2 ~{reads_2} \
      --out1 outputs/clean_R1.fastq.gz \
      --out2 outputs/clean_R2.fastq.gz \
      --html outputs/fastp.html \
      --json outputs/fastp.json \
      --thread ~{threads}
  >>>

  output {
    File clean_reads_1 = "outputs/clean_R1.fastq.gz"
    File clean_reads_2 = "outputs/clean_R2.fastq.gz"
    File html_report = "outputs/fastp.html"
    File json_report = "outputs/fastp.json"
  }

  runtime {
    docker: "quay.io/biocontainers/fastp:0.23.4--h5f740d0_0@sha256:b635334b6bb25eba14d0b8c240a45a51234984247d79715f8cd0b7959df850c2"
    cpu: 2
    memory: "4 GiB"
  }
}

task bwa_mem_runtime {
  input {
    File reads_1
    File reads_2
    File reference
    Int threads = 2
  }

  command <<<
    set -euo pipefail
    mkdir -p outputs
    cp ~{reference} reference.fa
    bwa index reference.fa
    bwa mem -t ~{threads} reference.fa ~{reads_1} ~{reads_2} > outputs/aligned.sam
  >>>

  output {
    File aligned_sam = "outputs/aligned.sam"
  }

  runtime {
    docker: "quay.io/biocontainers/bwa:0.7.17--he4a0461_11@sha256:652ca694adcb54ca799c22b843c086d570875ef14334a90ffeab0e1beb5f5741"
    cpu: 2
    memory: "4 GiB"
  }
}

task samtools_bam_runtime {
  input {
    File aligned_sam
    Int threads = 2
  }

  command <<<
    set -euo pipefail
    mkdir -p outputs
    samtools view -@ ~{threads} -b -o outputs/aligned.bam ~{aligned_sam}
    samtools quickcheck -v outputs/aligned.bam
  >>>

  output {
    File aligned_bam = "outputs/aligned.bam"
  }

  runtime {
    docker: "quay.io/biocontainers/samtools:1.20--h50ea8bc_1@sha256:bf80e07e650becfd084db1abde0fe932b50f990a07fa56421ea647b552b5a406"
    cpu: 2
    memory: "2 GiB"
  }
}

workflow fastp_bwa_runtime {
  input {
    File input_reads_1
    File input_reads_2
    File input_reference
  }

  call fastp_runtime {
    input:
      reads_1 = input_reads_1,
      reads_2 = input_reads_2
  }

  call bwa_mem_runtime {
    input:
      reads_1 = fastp_runtime.clean_reads_1,
      reads_2 = fastp_runtime.clean_reads_2,
      reference = input_reference
  }

  call samtools_bam_runtime {
    input:
      aligned_sam = bwa_mem_runtime.aligned_sam
  }

  output {
    File output_aligned_bam = samtools_bam_runtime.aligned_bam
    File output_fastp_html = fastp_runtime.html_report
    File output_fastp_json = fastp_runtime.json_report
  }
}
