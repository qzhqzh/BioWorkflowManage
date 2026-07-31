version 1.0

task bwa_mem {
  input {
    File reads_1
    File reads_2
    File reference
    Int threads = 8
  }

  command <<<
    set -euo pipefail
    mkdir -p outputs
    bwa mem -t ~{threads} ~{reference} ~{reads_1} ~{reads_2} | samtools view -b -o outputs/aligned.bam -
  >>>

  output {
    File aligned_bam = "outputs/aligned.bam"
  }

  runtime {
    docker: "quay.io/biocontainers/bwa:0.7.17--he4a0461_11"
    cpu: 8
    memory: "16 GB"
  }
}

task fastp {
  input {
    File reads_1
    File reads_2
    Int threads = 4
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
    docker: "quay.io/biocontainers/fastp:0.23.4--h5f740d0_0"
    cpu: 4
    memory: "8 GB"
  }
}

workflow fastp_bwa_demo {
  input {
    File input_reads_1
    File input_reads_2
    File input_reference
  }

  call fastp as fastp_1 {
    input:
      reads_1 = input_reads_1,
      reads_2 = input_reads_2,
      threads = 4
  }

  call bwa_mem as bwa_mem_1 {
    input:
      reads_1 = fastp_1.clean_reads_1,
      reads_2 = fastp_1.clean_reads_2,
      reference = input_reference,
      threads = 8
  }

  output {
    File output_aligned_bam = bwa_mem_1.aligned_bam
  }
}
