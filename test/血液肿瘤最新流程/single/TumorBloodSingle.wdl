version 1.0

import "SolidTumorSingle.wdl"

workflow TumorBloodSingle {
    input {
        String sample
        String sample_type
        String ref_version = "hg38"
        String sample_gender
        String project
        File fastq1
        File fastq2
        File bed
        File gene_bed
        File genome_bed = "oss://oss-zhenyuan-db/hg38/blood_tumor/bed/genome_windows.bed"
        File gene = "oss://oss-zhenyuan-db/hg38/annotation/gene_id.txt"
        File rep_trans = "oss://oss-zhenyuan-db/hg38/annotation/representative_transcript.txt"
        File cytoband = "oss://oss-zhenyuan-db/hg38/annotation/cytoBandIdeo.txt.gz"
        String local_frequency = "/easygene_data/common_db/local_frequency"
        Array[String] cnvkit_db_backbone_all = ["/easygene_data/hg38/blood_tumor/cnvkit_new0919/624panel_with_100kb_backbone", "/easygene_data/hg38/blood_tumor/cnvdb/624panel_backbone/v2/baseline_backbone_zm_kz"]
        Array[String] cnvkit_db_panel_84 = ["/easygene_data/hg38/blood_tumor/cnvkit/84panel", "/easygene_data/hg38/blood_tumor/cnvdb/84panel/baseline_zm_kz"]
        Array[String] cnvkit_db_panel_396 = ["/easygene_data/hg38/blood_tumor/cnvkit/396", "/easygene_data/hg38/blood_tumor/cnvdb/624panel_backbone/v2/baseline_624panel_zm_kz"]
        Array[String] cnvkit_db_panel_624 = ["/easygene_data/hg38/blood_tumor/cnvkit_new0919/624panel", "/easygene_data/hg38/blood_tumor/cnvdb/624panel_backbone/v2/baseline_624panel_zm_kz"]
        Array[File] refseq = ["oss://oss-zhenyuan-db/hg38/annotation/ncbiRefSeqCurated.txt.gz","oss://oss-zhenyuan-db/hg38/annotation/ncbiRefSeqCurated.txt.gz.tbi"]
        Array[File] dgv = ["oss://oss-zhenyuan-db/hg38/annotation/DGV_20200225.txt.gz", "oss://oss-zhenyuan-db/hg38/annotation/DGV_20200225.txt.gz.tbi"]
        Array[File] decipher = ["oss://oss-zhenyuan-db/hg38/annotation/decipher_population_cnv_grch38.txt.gz", "oss://oss-zhenyuan-db/hg38/annotation/decipher_population_cnv_grch38.txt.gz.tbi"]
        Array[File] cnv_clinvar = ["oss://oss-zhenyuan-db/hg38/annotation/variant_summary.txt.gz", "oss://oss-zhenyuan-db/hg38/annotation/variant_summary.txt.gz.tbi"]
        Array[File] cnv_clingen = ["oss://oss-zhenyuan-db/hg38/annotation/clingen_cnv.tsv.gz", "oss://oss-zhenyuan-db/hg38/annotation/clingen_cnv.tsv.gz.tbi"]

        Array[String] genomes =  [
            "/easygene_data/hg38/reference/Homo_sapiens_assembly38.fasta",
            "/easygene_data/hg38/reference/Homo_sapiens_assembly38.fasta.fai",
            "/easygene_data/hg38/reference/Homo_sapiens_assembly38.dict",
            "/easygene_data/hg38/reference/Homo_sapiens_assembly38.fasta.64.amb",
            "/easygene_data/hg38/reference/Homo_sapiens_assembly38.fasta.64.ann",
            "/easygene_data/hg38/reference/Homo_sapiens_assembly38.fasta.64.bwt",
            "/easygene_data/hg38/reference/Homo_sapiens_assembly38.fasta.64.pac",
            "/easygene_data/hg38/reference/Homo_sapiens_assembly38.2bit",
            "/easygene_data/hg38/reference/Homo_sapiens_assembly38.fasta.64.sa",
        ]
        String humandb = "/easygene_data/hg38/blood_tumor/humandb/"
        String sample_info
        String sample_info_new
        String sample_info_list
        String sample_info_list_new
        String output_dir
    }

    String dnafusion_local_freqdb = "${local_frequency}/${project}/frequency_db/${project}_dnafusion.mutation_frequency.txt"
    String snv_somatic_local_freqdb = "${local_frequency}/${project}/frequency_db/${project}_snv_somatic.mutation_frequency.txt"
    String snv_germline_local_freqdb = "${local_frequency}/${project}/frequency_db/${project}_snv_germline.mutation_frequency.txt"
    String cnv_local_freq_backbone = "${local_frequency}/${project}/frequency_db/local_freq.${project}.backbone.cnv.txt.gz"
    String cnv_local_freq_panel = "${local_frequency}/${project}/frequency_db/local_freq.${project}.panel.cnv.txt.gz"

    String dnafusion_local_freq_84panel = "/easygene_data/hg38/blood_tumor/resource/local_freq_blood/dna_fusion/84fusion.mutation_frequency.txt"
    String dnafusion_local_freq_624panel = "/easygene_data/hg38/blood_tumor/resource/local_freq_blood/dna_fusion/624fusion.mutation_frequency.txt"
    String fq_path = fastq1
    String cnvkit_db_backbone = if sub(fq_path, "zy_zm", "") != fq_path then cnvkit_db_backbone_all[1] else cnvkit_db_backbone_all[0]
    Array[String] cnvkit_db_panel_select = if sub(basename(bed), "84panel", "") != basename(bed) then cnvkit_db_panel_84 else if sub(basename(bed), "396", "") != basename(bed) then cnvkit_db_panel_396 else cnvkit_db_panel_624
    String cnvkit_db_panel = if sub(fq_path, "zy_zm", "") != fq_path then cnvkit_db_panel_select[1] else cnvkit_db_panel_select[0]
    Boolean is_84 = if sub(basename(bed), "84panel", "") != basename(bed) then true else false
    Boolean is_MRD = if sub(basename(bed), "MRD", "") != basename(bed) then true else false
    String dnafusion_local_freq = if is_84  then dnafusion_local_freq_84panel else dnafusion_local_freq_624panel

    call SolidTumorSingle.QC {
        input:
        sample = sample,
        fastq = (fastq1, fastq2),
    }

    call SolidTumorSingle.Align {
        input:
        sample = sample,
        genomes = genomes,
        cleaned_fastq = QC.cleaned_fastq,
    }

    call Flt3_ITD {
        input:
        sample = sample,
        ref_version = ref_version,
        sorted_bam = Align.align_bam
    }

    call LumpySV {
        input:
        sample = sample,
        sorted_bam = Align.align_bam
    }

    call SolidTumorSingle.CollectHsMetrics {
        input:
        sample = sample,
        genomes = genomes,
        bed = bed,
        align_bam = Align.align_bam,
    }

    call SolidTumorSingle.DeDup {
        input:
        sample = sample,
        genomes = genomes,
        bed = bed,
        align_bam = Align.align_bam
    }

    call CollectHsMetricsDeDup {
        input:
        sample = sample,
        genomes = genomes,
        bed = bed,
        dedup_bam = DeDup.dedup_bam,
    }

    call SolidTumorSingle.CallSNV {
        input:
        sample = sample,
        genomes = genomes,
        bed = bed,
        genome_bed = genome_bed,
        is_MRD = is_MRD,
        dedup_bam = DeDup.dedup_bam,
    }

    call SolidTumorSingle.AnnoSNV {
        input:
        sample = sample,
        ref_version = ref_version,
        humandb = humandb,
        vcf = CallSNV.snv,
    }

    call SolidTumorSingle.CallFusion {
        input:
        sample = sample,
        genomes = genomes,
        cleaned_fastq = QC.cleaned_fastq_genefuse,
    }

    if(!is_84){
        call HLAGenotyping {
            input:
            sample = sample,
            align_bam = Align.align_bam,
        }

        call HLA_LA {
            input:
            sample = sample,
            align_bam = Align.align_bam,
        }

        call AutoCNVKit_Backbone {
            input:
            prefix = sample,
            cytoband = cytoband,
            dgv = dgv,
            decipher = decipher,
            clinvar = cnv_clinvar,
            clingen = cnv_clingen,
            local_freq = cnv_local_freq_backbone,
            refseq = refseq,
            gene = gene,
            rep_trans = rep_trans,
            sorted_bam = DeDup.dedup_bam,
            cnvkit_db = cnvkit_db_backbone,
            sample_gender = sample_gender
        }

        call IntegrateSample_Backbone {
            input:
            prefix = sample,
            gender = AutoCNVKit_Backbone.gender,
            cnv_vcf = AutoCNVKit_Backbone.vcf,
            cnv_plot = AutoCNVKit_Backbone.plot
        }
    }

    call AutoCNVKit_Panel {
        input:
        prefix = sample,
        cytoband = cytoband,
        dgv = dgv,
        decipher = decipher,
        clinvar = cnv_clinvar,
        clingen = cnv_clingen,
        local_freq = cnv_local_freq_panel,
        refseq = refseq,
        gene = gene,
        rep_trans = rep_trans,
        sorted_bam = DeDup.dedup_bam,
        cnvkit_db = cnvkit_db_panel,
        sample_gender = sample_gender
    }

    call IntegrateSample_Panel {
        input:
        prefix = sample,
        gender = AutoCNVKit_Panel.gender,
        cnv_vcf = AutoCNVKit_Panel.vcf,
        cnv_plot = AutoCNVKit_Panel.plot
    }

    call Collect {
        input:
        sample = sample,
        sample_info = sample_info,
        sample_info_new = sample_info_new,
        sample_info_list = sample_info_list,
        sample_info_list_new = sample_info_list_new,
        ref_version = ref_version,
        sample_type = sample_type,
        genomes = genomes,
        humandb = humandb,
        sample_gender = sample_gender,
        refseq = refseq,
        dnafusion_local_freq = dnafusion_local_freq,
        dnafusion_local_freqdb = dnafusion_local_freqdb,
        snv_somatic_local_freqdb = snv_somatic_local_freqdb,
        snv_germline_local_freqdb = snv_germline_local_freqdb,
        anno_vcf = AnnoSNV.anno_vcf,
        genefuse = CallFusion.fusion_text,
        fusion_html = CallFusion.fusion_html,
        backbone_cn = AutoCNVKit_Backbone.cn,
        backbone_cnv_zip = IntegrateSample_Backbone.backbone_cnv_zip,
        panel_cn = AutoCNVKit_Panel.cn,
        panel_cnv_zip = IntegrateSample_Panel.panel_cnv_zip,
        align_bam = Align.align_bam,
        output_dir = output_dir,
        fastp_json = QC.fastp_json,
        fastp_html = QC.fastp_html,
        coverage = CollectHsMetrics.coverage,
        bamdst_report = CollectHsMetrics.bamdst_report,
        coverage_dedup = CollectHsMetricsDeDup.coverage_dedup,
        bamdst_report_dedup = CollectHsMetricsDeDup.bamdst_report_dedup,
        hla_tsv = HLAGenotyping.hla_tsv,
        hla_la_tsv = HLA_LA.hla_la_tsv,
        sv_vcf = LumpySV.sv_vcf,
        flt3_vcf = Flt3_ITD.flt3_vcf,
        flt3_summary = Flt3_ITD.flt3_summary
    }

    output {
        File qc_json = QC.fastp_json
        File qc_html = QC.fastp_html
        File? hla_tsv = Collect.all_hla_tsv
        File sv_vcf = LumpySV.sv_vcf
        Pair[File, File]? split_bam = LumpySV.split_bam
        Pair[File, File]? discordant_bam = LumpySV.discordant_bam
        Pair[File, File] dedup_bam =  DeDup.dedup_bam
        File coverage = CollectHsMetrics.coverage
        File bamdst_report = CollectHsMetrics.bamdst_report
        File coverage_dedup = CollectHsMetricsDeDup.coverage_dedup
        File bamdst_report_dedup = CollectHsMetricsDeDup.bamdst_report_dedup
        File snv = CallSNV.snv
        File anno_vcf = AnnoSNV.anno_vcf
        File? auto_cnv_backbone = AutoCNVKit_Backbone.cn
        File? cnv_zip_backbone = Collect.cnv_zip_backbone
        File? auto_cnv_vcf_backbone = AutoCNVKit_Backbone.vcf
        File auto_cnv_panel = AutoCNVKit_Panel.cn
        File cnv_zip_panel = Collect.cnv_zip_panel
        File auto_cnv_vcf_panel = AutoCNVKit_Panel.vcf
        File? cnv_zip_backbone_plot = AutoCNVKit_Backbone.plot
        File cnv_zip_panel_plot = AutoCNVKit_Panel.plot
        File fusion_html = CallFusion.fusion_html
        File fusion_json = CallFusion.fusion_json
        File fusion_text = CallFusion.fusion_text
        File anno_filter_germline_xls = Collect.anno_filter_germline_xls
        File anno_germline_xls = Collect.anno_germline_xls
        File anno_filter_xls = Collect.anno_filter_xls
        File anno_xls = Collect.anno_xls
        File collect_sample_info = Collect.collect_sample_info
        File filter_gene_fusion = Collect.filter_gene_fusion
        File sv_fusion = Collect.sv_fusion
        File total_fusion = Collect.total_fusion
        File qc_stat = Collect.qc_stat
        File qc_stat_dedup = Collect.qc_stat_dedup
        File sample_task_json = Collect.sample_task_json
        File chemo_xls = Collect.chemo_xls
        File sample_zip = Collect.sample_zip
        File import_zip = Collect.import_zip
    }
}

task SeqkitRmdupR {
    input {
        String docker = 'registry.cn-shanghai.aliyuncs.com/kszy-biosoft/seqkit:v1.0'
        Int cpu = 8
        String memory = "32G"
        String disk = "100G"
        String sample
        Pair[File, File] cleaned_fastq
    }
    command {
        set -vex
        seqkit rmdup -s ${cleaned_fastq.left} -o ${sample}.rudup.r1.fq.gz && \
        seqkit rmdup -s ${cleaned_fastq.right} -o ${sample}.rudup.r2.fq.gz
    }
    output {
        Pair[File, File] redup_fq = ("${sample}.rudup.r1.fq.gz", "${sample}.rudup.r2.fq.gz")
    }
    runtime {
        docker: docker
        cpu: cpu
        memory: memory
        disk: disk
    }
}

task HLAGenotyping {
    input {
        String docker = 'registry.cn-shanghai.aliyuncs.com/kszy-biosoft/hla-hd:v1.7.0'
        String sample
        Pair[File, File] align_bam
        String parma = " -m 90 -c 0.95 "
        Int cpu = 16
        String memory = "64G"
        String disk = "400G"
    }
    Float align_bam_size = size(align_bam.left,"GB")
    Boolean seqkt_t = if align_bam_size > 5 then true else false
    command {
        set -vex
        samtools view -@ ${cpu} ${align_bam.left} chr6:28510120-33480577 -b > ${sample}.HLA.bam && \
        samtools view -@ ${cpu} ${align_bam.left} -bh -f 12 > ${sample}.unmapped.bam && \
        samtools merge -@ ${cpu} ${sample}.HLA.merge.bam ${sample}.HLA.bam ${sample}.unmapped.bam && \
        samtools sort -@ ${cpu} -n ${sample}.HLA.merge.bam -o ${sample}.HLA.sort.bam && \
        samtools fastq ${sample}.HLA.sort.bam \
        -1 ${sample}.HLA.R1.fastq \
        -2 ${sample}.HLA.R2.fastq \
        -s /dev/null \
        -@ ${cpu} && \
        sed -i 's/:UMI_[A-Z]*_[A-Z]*//g' ${sample}.HLA.R1.fastq && \
        sed -i 's/:UMI_[A-Z]*_[A-Z]*//g' ${sample}.HLA.R2.fastq && \
        if ${seqkt_t}; then
            /seqtk/seqtk sample -s100 ${sample}.HLA.R1.fastq 0.2 > ${sample}.HLA.R1.0.2.fastq && \
            /seqtk/seqtk sample -s100 ${sample}.HLA.R2.fastq 0.2 > ${sample}.HLA.R2.0.2.fastq && \
            mv ${sample}.HLA.R1.0.2.fastq ${sample}.HLA.R1.fastq && \
            mv ${sample}.HLA.R2.0.2.fastq ${sample}.HLA.R2.fastq
        fi
        mkdir hla_result && \
        hlahd.sh \
        -t ${cpu} \
        ${parma} \
        -f /data2/hlahd.1.7.0/freq_data \
        ${sample}.HLA.R1.fastq \
        ${sample}.HLA.R2.fastq \
        /data2/hlahd.1.7.0/HLA_gene.split.txt \
        /data2/hlahd.1.7.0/dictionary \
        ${sample} \
        hla_result && \
        python3 /scripts/hlahd_sort.py -i hla_result/${sample}/result -o ${sample}.hla.tsv
    }
    output {
        File hla_tsv = "${sample}.hla.tsv"
    }
    runtime {
        docker: docker
        cpu: cpu
        memory: memory
        disk: disk
    }
}

task xHLA {
    input {
        String docker = 'registry.cn-shanghai.aliyuncs.com/kszy-biosoft/xhla:latest'
        String sample
        Pair[File, File] align_bam
        Int cpu = 24
        String memory = "96G"
        String disk = "200G"
    }
    command {
        set -vex
        samtools view -@ ${cpu} ${align_bam.left} chr6:28510120-33480577 -b > ${sample}.HLA.bam && \
        samtools view -@ ${cpu} ${align_bam.left} -bh -f 12 > ${sample}.unmapped.bam && \
        samtools merge -@ ${cpu} ${sample}.HLA.merge.bam ${sample}.HLA.bam ${sample}.unmapped.bam && \
        samtools sort -@ ${cpu} ${sample}.HLA.merge.bam -o ${sample}.HLA.sort.bam && \
        samtools index ${sample}.HLA.sort.bam && \
        mkdir ${sample} && \
        run.py --sample_id ${sample} --input_bam_path ./${sample}.HLA.sort.bam --output_path ${sample} && \
        python3 /scripts/extract_hla.py -i ${sample}/*.json -o ${sample}.xhla.tsv
    }
    output {
        File xhla_tsv = "${sample}.xhla.tsv"
    }
    runtime {
        docker: docker
        cpu: cpu
        memory: memory
        disk: disk
    }
}

task HLA_LA {
    input {
        String docker = 'registry.cn-shanghai.aliyuncs.com/kszy-biosoft/hla-la:latest'
        String sample
        String database = "/easygene_data/hg38/hla_reference/"
        Pair[File, File] align_bam
        Int cpu = 12
        String memory = "96G"
        String disk = "200G"
    }
    Float align_bam_size = size(align_bam.left,"GB")
    Float extract_ratio = if align_bam_size > 5 then 0.1 else 0.2
    command {
        set -vex
        samtools view -@ ${cpu} ${align_bam.left} chr6:28510120-33480577 -b > ${sample}.HLA.bam && \
        samtools view -@ ${cpu} ${align_bam.left} -bh -f 12 > ${sample}.unmapped.bam && \
        samtools merge -@ ${cpu} ${sample}.HLA.merge.bam ${sample}.HLA.bam ${sample}.unmapped.bam && \
        samtools sort -@ ${cpu} ${sample}.HLA.merge.bam -o ${sample}.HLA.sort.bam && \
        samtools index ${sample}.HLA.sort.bam && \
        /usr/local/bin/HLA-LA/src/HLA-LA.pl \
        --BAM ${sample}.HLA.sort.bam \
        --graph ${database} \
        --sampleID ${sample} \
        --maxThreads ${cpu} \
        --extract_rtio ${extract_ratio} \
        --workingDir ./ && \
        mv ${sample}/hla/R1_bestguess_G.txt ${sample}.R1_bestguess_G.txt && \
        mv ${sample}/hla/R1_bestguess.txt ${sample}.R1_bestguess.txt && \
        python3 /scripts/extract_hla.py -i ${sample}.R1_bestguess_G.txt -o ${sample}.hla_la.tsv
    }
    output {
        File hla_la_tsv = "${sample}.hla_la.tsv"
    }
    runtime {
        docker: docker
        cpu: cpu
        memory: memory
        disk: disk
        env: {
            "BUCKET": "oss-zhenyuan-db"
        }
    }
}

task Flt3_ITD {
    input {
        String docker = 'registry.cn-shanghai.aliyuncs.com/kszy-biosoft/flt3_itd_ext:latest'
        String sample
        String ref_version
        Pair[File, File] sorted_bam
        Int cpu = 4
        String memory = "16G"
        String disk = "100G"
    }
    command {
        set -vex
        python /biosoft/FLT3_ITD_ext/FLT3_ITD.py -b ${sorted_bam.left} -g ${ref_version} && \
        if [[ -e "${sample}.sorted_FLT3_ITD.vcf" ]]
        then
            mv ${sample}.sorted_FLT3_ITD.vcf ${sample}_FLT3_ITD.vcf
        else
            touch ${sample}_FLT3_ITD.vcf
        fi
        if [[ -e "${sample}.sorted_FLT3_ITD_summary.txt" ]]
        then
            mv ${sample}.sorted_FLT3_ITD_summary.txt ${sample}_FLT3_ITD_summary.xls
        else
            touch ${sample}_FLT3_ITD_summary.xls
        fi
    }
    output {
        File flt3_vcf = "${sample}_FLT3_ITD.vcf"
        File flt3_summary = "${sample}_FLT3_ITD_summary.xls"
    }
    runtime {
        docker: docker
        cpu: cpu
        memory: memory
        disk: disk
        env: {
            "BUCKET": "oss-zhenyuan-db"
        }
    }
}

task LumpySV {
    input {
        String docker = 'registry.cn-shanghai.aliyuncs.com/kszy-biosoft/lumpy-sv:v1'
        String sample
        Pair[File, File] sorted_bam
        Int cpu = 12
        String memory = "24G"
        String disk = "200G"
    }
    command {
        set -vex
        samtools sort -@ ${cpu} -n -O SAM ${sorted_bam.left} | \
        samblaster --excludeDups --addMateTags --maxSplitCount 2 --minNonOverlap 20 | samtools view -@ ${cpu} -S -b -o ${sample}.lumpy.bam - && \
        samtools view -@ ${cpu} -b -F 1294 ${sample}.lumpy.bam | samtools sort -@ ${cpu} -o ${sample}.discordants.sorted.bam - && \
        samtools view -@ ${cpu} -h ${sample}.lumpy.bam | /usr/local/bin/lumpy-sv/scripts/extractSplitReads_BwaMem -i stdin | \
        samtools view -@ ${cpu} -Sb - | samtools sort -@ ${cpu} -o ${sample}.splitters.sorted.bam - && \
        lumpyexpress -B ${sample}.lumpy.bam -S ${sample}.splitters.sorted.bam -D ${sample}.discordants.sorted.bam -o ${sample}.sv.vcf && \
        samtools index ${sample}.splitters.sorted.bam && \
        samtools index ${sample}.discordants.sorted.bam
    }
    output {
        File sv_vcf = "${sample}.sv.vcf"
        Pair[File, File] split_bam = ("${sample}.splitters.sorted.bam", "${sample}.splitters.sorted.bam.bai")
        Pair[File, File] discordant_bam = ("${sample}.discordants.sorted.bam", "${sample}.discordants.sorted.bam.bai")
    }
    runtime {
        docker: docker
        cpu: cpu
        memory: memory
        disk: disk
        env: {
            "BUCKET": "oss-zhenyuan-db"
        }
    }
}

task CollectHsMetricsDeDup {
    input {
        String docker = 'registry.cn-shanghai.aliyuncs.com/kszy-biosoft/qc_stat:2.18.1'
        String sample
        Int cpu = 2
        String memory = "8G"
        String disk = "100G"
        Pair[File, File] dedup_bam
        Array[String] genomes
        File bed
    }
    command {
        set -vex
        mkdir ${sample}_bamdst_res && \
        /bamdst/bamdst -p ${bed} -o ${sample}_bamdst_res ${dedup_bam.left} && \
        mv ${sample}_bamdst_res/coverage.report ${sample}_dedup_coverage.report && \
        mv ${sample}_bamdst_res/depth.tsv.gz ${sample}_dedup_depth.tsv.gz && \
        gunzip ${sample}_dedup_depth.tsv.gz
    }

    output {
        File coverage_dedup = "${sample}_dedup_depth.tsv"
        File bamdst_report_dedup = "${sample}_dedup_coverage.report"
    }

    runtime {
        docker: docker
        cpu: cpu
        memory: memory
        disk: disk
        env: {
            "BUCKET": "oss-zhenyuan-db"
        }
    }
}

task AutoCNVKit_Backbone {
    input {
        String docker = 'registry.cn-shanghai.aliyuncs.com/kszy-biosoft/integrate_cnv_blood:latest'
        Int cpu = 4
        String memory = "16G"
        String disk = "100G"
        String prefix
        String sample_gender
        File cytoband
        Array[File] dgv
        Array[File] decipher
        Array[File] clinvar
        Array[File] refseq
        Array[File] clingen
        String local_freq
        File gene
        File rep_trans
        Pair[File, File] sorted_bam
        String cnvkit_db
        String? cnv_opts
    }
    command {
        set -vex
        if [[ "${sample_gender}" == *男* || "${sample_gender}" == "male" ]]
        then
            echo "male" > ${prefix}.gender.txt
        else
            echo "female" > ${prefix}.gender.txt
        fi
        /usr/local/bin/_entrypoint.sh auto_cnvkit \
        -i ${sorted_bam.left} \
        -s ${prefix} \
        -o . \
        -C ${cytoband} \
        -G1 ${refseq[0]} \
        -G2 ${gene} \
        -G3 ${rep_trans} \
        -D1 ${dgv[0]} \
        -D2 ${decipher[0]} \
        -D3 ${clinvar[0]} \
        -D4 ${clingen[0]} \
        -D5 ${local_freq} \
        -T ${cnvkit_db}/bed/targets.bed \
        -A ${cnvkit_db}/bed/antitargets.bed \
        -M ${cnvkit_db}/reference/male.ref.cnn \
        -F ${cnvkit_db}/reference/female.ref.cnn \
        -MD ${cnvkit_db}/fix/male/ \
        -FD ${cnvkit_db}/fix/female/ \
        -l 0.7 \
        -m hmm-tumor \
        -w 2 \
        -t="-1.1|-0.15|0.14|0.7" \
        ${if defined(cnv_opts) && cnv_opts != "" then "-nc " + cnv_opts else ""}
        if [[ "${sample_gender}" == *男* || "${sample_gender}" == *女* ]]
        then
            mv ${prefix}.cnv.vcf ${prefix}.backbone.cnv.vcf && \
            mv ${prefix}.cn.tsv ${prefix}.backbone.cn.tsv && \
            mv ${prefix}_cnv_plot.zip ${prefix}.backbone_cnv_plot.zip
        else
            echo "sex undefinited"
        fi
    }
    output {
        File vcf = "${prefix}.backbone.cnv.vcf"
        Array[File] cnn =  ["${prefix}.targets.cov.cnn", "${prefix}.antitargets.cov.cnn"]
        File cnr = "${prefix}.cnr"
        File cn = "${prefix}.backbone.cn.tsv"
        File plot = "${prefix}.backbone_cnv_plot.zip"
        String gender = read_string("${prefix}.gender.txt")
    }
    runtime {
        docker: docker
        cpu: cpu
        memory: memory
        disk: disk
        env: {
            "BUCKET": "oss-zhenyuan-db"
        }
    }
}

task AutoCNVKit_Panel {
    input {
        String docker = 'registry.cn-shanghai.aliyuncs.com/kszy-biosoft/integrate_cnv_blood:latest'
        Int cpu = 4
        String memory = "8G"
        String disk = "100G"
        String prefix
        String sample_gender
        File cytoband
        Array[File] dgv
        Array[File] decipher
        Array[File] clinvar
        Array[File] refseq
        Array[File] clingen
        String local_freq
        File gene
        File rep_trans
        Pair[File, File] sorted_bam
        String cnvkit_db
        String? cnv_opts
    }
    command {
        set -vex
        if [[ "${sample_gender}" == *男* || "${sample_gender}" == "male" ]]
        then
            echo "male" > ${prefix}.gender.txt
        else
            echo "female" > ${prefix}.gender.txt
        fi
        /usr/local/bin/_entrypoint.sh auto_cnvkit \
        -i ${sorted_bam.left} \
        -s ${prefix} \
        -o . \
        -C ${cytoband} \
        -G1 ${refseq[0]} \
        -G2 ${gene} \
        -G3 ${rep_trans} \
        -D1 ${dgv[0]} \
        -D2 ${decipher[0]} \
        -D3 ${clinvar[0]} \
        -D4 ${clingen[0]} \
        -D5 ${local_freq} \
        -T ${cnvkit_db}/bed/targets.bed \
        -A ${cnvkit_db}/bed/antitargets.bed \
        -M ${cnvkit_db}/reference/male.ref.cnn \
        -F ${cnvkit_db}/reference/female.ref.cnn \
        -MD ${cnvkit_db}/fix/male/ \
        -FD ${cnvkit_db}/fix/female/ \
        -m hmm-tumor \
        -l 0.7 \
        -w 2 \
        -sp \
        -mp 1 \
        -t="-1.1|-0.15|0.14|0.7" \
        -gc="0.2|0.8" \
        ${if defined(cnv_opts) && cnv_opts != "" then "-nc " + cnv_opts else ""}
        if [[ "${sample_gender}" == *男* || "${sample_gender}" == *女* ]]
        then
            mv ${prefix}.cnv.vcf ${prefix}.panel.cnv.vcf && \
            mv ${prefix}.cn.tsv ${prefix}.panel.cn.tsv && \
            mv ${prefix}_cnv_plot.zip ${prefix}.panel_cnv_plot.zip
        else
            echo "sex undefinited"
        fi
    }
    output {
        File vcf = "${prefix}.panel.cnv.vcf"
        Array[File] cnn =  ["${prefix}.targets.cov.cnn", "${prefix}.antitargets.cov.cnn"]
        File cnr = "${prefix}.cnr"
        File cn = "${prefix}.panel.cn.tsv"
        File plot = "${prefix}.panel_cnv_plot.zip"
        String gender = read_string("${prefix}.gender.txt")
    }
    runtime {
        docker: docker
        cpu: cpu
        memory: memory
        disk: disk
        env: {
            "BUCKET": "oss-zhenyuan-db"
        }
    }
}

task IntegrateSample_Backbone {
    input {
        String docker = 'registry.cn-shanghai.aliyuncs.com/kszy-biosoft/integrate_germline:latest'
        Int cpu = 2
        String memory = "8G"
        String disk = "100G"
        String prefix
        String gender
        File cnv_vcf
        File cnv_plot
    }
    command {
        set -vex
        integrate_germline sample \
        -n ${prefix} \
        -g ${gender} \
        -o ${prefix}.backbone_cnv.zip \
        -c ${cnv_vcf} \
        -cp ${cnv_plot}
    }
    output {
        File backbone_cnv_zip = "${prefix}.backbone_cnv.zip"
    }
    runtime {
        docker: docker
        cpu: cpu
        memory: memory
        disk: disk
    }
}

task IntegrateSample_Panel {
    input {
        String docker = 'registry.cn-shanghai.aliyuncs.com/kszy-biosoft/integrate_germline:latest'
        Int cpu = 2
        String memory = "4G"
        String disk = "100G"
        String prefix
        String gender
        File cnv_vcf
        File cnv_plot
    }
    command {
        set -vex
        integrate_germline sample \
        -n ${prefix} \
        -g ${gender} \
        -o ${prefix}.panel_cnv.zip \
        -c ${cnv_vcf} \
        -cp ${cnv_plot}
    }
    output {
        File panel_cnv_zip = "${prefix}.panel_cnv.zip"
    }
    runtime {
        docker: docker
        cpu: cpu
        memory: memory
        disk: disk
    }
}

task Collect {
    input {
        String docker = 'registry.cn-shanghai.aliyuncs.com/kszy-biosoft/collect:latest'
        Int cpu = 4
        String memory = "32G"
        String disk = "100G"
        String sample
        String sample_name
        String ref_version
        String sample_type
        Array[String] genomes
        String humandb
        String sample_gender
        Array[File] refseq
        File anno_vcf
        File genefuse
        File fusion_html
        File panel_cn
        File? backbone_cn
        File? backbone_cnv_zip
        File panel_cnv_zip
        Pair[File, File] align_bam
        String copy_number_gt_threshold = "5"
        File key_site = "oss://oss-zhenyuan-db/hg19/resource/combine.tsv"
        File gene_list
        File gene_transcript_matchup = "oss://oss-zhenyuan-db/hg38/blood_tumor/resource/sorted.gene.tx.blood.txt"
        File hotspot_gene = "oss://oss-zhenyuan-db/hg19/resource/hotspot_gene-20230227.xls"
        File tumor_gene = "oss://oss-zhenyuan-db/hg19/resource/tumor-gene-20241016.xlsx"
        File refgene = "oss://oss-zhenyuan-db/hg38/blood_tumor/humandb/hg38_refGeneWithVer.txt"
        File refgff = "oss://oss-zhenyuan-db/hg38/blood_tumor/resource/genomic.gene.gff"
        File ref_annot = "oss://oss-zhenyuan-db/hg38/blood_tumor/database/ref_annot.gtf"
        File rs_db = "oss://oss-zhenyuan-db/hg38/blood_tumor/resource/20231220/rs.uniq-20231218.in"
        File chemo_db = "oss://oss-zhenyuan-db/hg38/blood_tumor/resource/20231220/chemo_efficacy_toxicity_database.sorted.txt"
        File cnv_tumor_gene = "oss://oss-zhenyuan-db/hg38/blood_tumor/resource/cnv_tumor_gene.2024-2.xlsx"
        File snv_filterfile = "oss://oss-zhenyuan-db/hg38/blood_tumor/resource/84panel_filter.xls"
        File snv_filterfile624 = "oss://oss-zhenyuan-db/hg38/blood_tumor/resource/624panel_anno_filter.xls"
        File panel624_genelist = "oss://oss-zhenyuan-db/hg38/blood_tumor/bed/kszy_BloodTumor_DNA_panel.624.gene.list"
        File panel84_genelist = "oss://oss-zhenyuan-db/hg38/blood_tumor/bed/kszy_84panel.hg38.gene.list"
        File dosage = "oss://oss-zhenyuan-db/hg38/blood_tumor/resource/dosage_sensitivity_gene.xlsx"
        File anno_db = "oss://oss-zhenyuan-db/hg38/blood_tumor/resource/local_freq_blood/local_freq_blood.zip"
        File ensembl_genbank = "oss://oss-zhenyuan-db/hg19/resource/ensembltogenbank.xls"
        File pre_class = "oss://oss-zhenyuan-db/hg38/blood_tumor/resource/pre_class.xlsx"
        File pre_class_cnv = "oss://oss-zhenyuan-db/hg38/blood_tumor/resource/panel_CNV_PreClass.xls"
        Array[File] lcr_sdr = ["oss://oss-zhenyuan-db/hg38/blood_tumor/resource/GRch38.repeats.coord_noseq.bed","oss://oss-zhenyuan-db/hg38/blood_tumor/resource/genomicSuperDups.bed"]
        File fusion_specific_region = "oss://oss-zhenyuan-db/hg38/blood_tumor/resource/special_region.xls"
        String sample_info
        String sample_info_new
        String sample_info_list
        String sample_info_list_new
        String output_dir
        File fastp_json
        File fastp_html
        File coverage
        File bamdst_report
        File coverage_dedup
        File bamdst_report_dedup
        File? hla_tsv
        File? hla_la_tsv
        File sv_vcf
        File flt3_vcf
        File flt3_summary
        String pattern1 = "*206panel*"
        String pattern2 = "*84panel*"
        String sv_rec1 = '{if($3=="-" || $6=="-")print}'
        String sv_rec2 = '{if($3!="-" && $6!="-")print}'
        String dnafusion_local_freq
        String dnafusion_local_freqdb
        String snv_somatic_local_freqdb
        String snv_germline_local_freqdb
    }
    command {
        set -vex
        export TRANSVAR_CFG=/transvar_database/transvar_hg38.cfg && \
        transvar config -k reference -v ${genomes[0]} --refversion hg38 && \

        echo ${sample_info} | base64 -d > ${sample}.sample.info.txt && \
        echo ${sample_info_new} | base64 -d > ${sample}.sample.info.new.txt && \
        echo ${sample_info_list} | base64 -d > ${sample}.sample.info.list.txt && \
        echo ${sample_info_list_new} | base64 -d > ${sample}.sample.info.list.new.txt && \

        python3 /scripts/pre_select_v2_hg38_blood.py -k ${key_site} -l ${gene_list} -s ${sample_type} -t ${gene_transcript_matchup} \
        -f ${anno_vcf} -o ${sample}.var.${ref_version}_multianno.filter.xls -g ${hotspot_gene}  -c ${tumor_gene} -r ${refgene} -r2 ${refgff} \
        -r3 ${snv_filterfile} -r4 ${snv_filterfile624} -r6 ${coverage} -le ${pre_class} -si ${sample}.sample.info.txt -lr "${lcr_sdr[0]}|${lcr_sdr[1]}" && \

        python3 /scripts/anno_local_freq.py \
        -a ${sample}.var.${ref_version}_multianno.all.xls \
        -a1 ${sample}.var.${ref_version}_multianno.filter.xls \
        -g ${sample}.var.${ref_version}_multianno.all.germline.xls \
        -g1 ${sample}.var.${ref_version}_multianno.filter.germline.xls \
        -l ${gene_list} \
        -t "${snv_somatic_local_freqdb},${snv_germline_local_freqdb}"   \
        -o1 ${sample}.var.${ref_version}_multianno.local_freq_anno.all.xls \
        -o2 ${sample}.var.${ref_version}_multianno.local_freq_anno.filter.xls \
        -o3 ${sample}.var.${ref_version}_multianno.local_freq_anno.all.germline.xls \
        -o4 ${sample}.var.${ref_version}_multianno.local_freq_anno.filter.germline.xls && \
        mv ${sample}.var.${ref_version}_multianno.local_freq_anno.all.xls ${sample}.var.${ref_version}_multianno.all.xls && \
        mv ${sample}.var.${ref_version}_multianno.local_freq_anno.filter.xls ${sample}.var.${ref_version}_multianno.filter.xls && \
        mv ${sample}.var.${ref_version}_multianno.local_freq_anno.all.germline.xls ${sample}.var.${ref_version}_multianno.all.germline.xls && \
        mv ${sample}.var.${ref_version}_multianno.local_freq_anno.filter.germline.xls ${sample}.var.${ref_version}_multianno.filter.germline.xls && \

        perl /scripts/sequencing_uniformity.bismark.pl ${coverage} > ${sample}.stat && \
        python /scripts/qc_statistical_result.bamdst.py ${fastp_json} ${bamdst_report} ${sample}.stat > ${sample}.qc_stat.xls && \
        perl /scripts/sequencing_uniformity.bismark.pl ${coverage_dedup} > ${sample}_dedup.stat && \
        python /scripts/qc_statistical_result.bamdst.py ${fastp_json} ${bamdst_report_dedup} ${sample}_dedup.stat > ${sample}.qc_stat_dedup.xls && \

        python3 /scripts/process_genefuse_res.py -g ${gene_list} -b ${align_bam.left} -f ${genefuse} -r ${refseq[0]} && \
        python /scripts/process_sv.py -f ${sv_vcf} -b ${align_bam.left} -r ${refseq[0]} -fai ${genomes[1]} -o ${sample}.sv.xls -ref ${ref_version} && \
        python /scripts/process_factera_res.py -g ${gene_list} -f ${sample}.sv.xls -r ${refseq[0]} -tx ${gene_transcript_matchup} -t L && \

        python /scripts/fusion_merge.py --prefix ${sample} --special_region ${fusion_specific_region} && \
        if [ -f "${dnafusion_local_freqdb}" ]; then
            python /scripts/RNA_local_freq_anno.py fusion -i ${sample}.total.fusion.xls -f ${dnafusion_local_freqdb}
        else
            python /scripts/RNA_local_freq_anno.py fusion -i ${sample}.total.fusion.xls -f ${dnafusion_local_freq}
        fi

        python /scripts/chemo_anno-2023218.py ${anno_vcf} ${rs_db} ${chemo_db} ${sample}.chemo.estimate.xls >> temp.txt && \

        ${"python /scripts/merge_hla.py -i " + hla_tsv + " " + hla_la_tsv + " -o ${sample}.hla.tsv"}

        if [[ "${gene_list}" != *84panel* ]]
        then
            unzip ${backbone_cnv_zip} -d backbone_cnv && cd backbone_cnv && \
            python /scripts/cnv_redo.py \
            -tg ${cnv_tumor_gene} \
            -cv CNV.tsv \
            -dg ${dosage} \
            -cn ${backbone_cn} \
            -cy ${humandb}/hg38_cytoBand.txt \
            -x ${sample_gender} \
            -o ${sample}.CNV_backbone.filter.tsv && \
            zip ../${sample}.backbone_cnv.zip * && cd ..
        else
            touch ${sample}.backbone_cnv.zip
        fi

        unzip ${panel_cnv_zip} -d panel_cnv && cd panel_cnv && \
        if [[ "${gene_list}" == ${pattern2} ]]
        then
            python /scripts/cnv_redo.py -tg ${cnv_tumor_gene} -cv CNV.tsv -o ${sample}.CNV_panel.filter.tsv \
            -pg ${panel84_genelist} -le ${pre_class_cnv} -dg ${dosage} -cn ${panel_cn} -cy ${humandb}/hg38_cytoBand.txt -x ${sample_gender}
        else
            python /scripts/cnv_redo.py -tg ${cnv_tumor_gene} -cv CNV.tsv -o ${sample}.CNV_panel.filter.tsv \
            -pg ${panel624_genelist} -le ${pre_class_cnv} -dg ${dosage} -cn ${panel_cn} -cy ${humandb}/hg38_cytoBand.txt -x ${sample_gender}
        fi
        zip ../${sample}.panel_cnv.zip * && cd .. && \

        mkdir ${sample} && \
        if [[ "${gene_list}" == ${pattern1} || "${gene_list}" == ${pattern2} ]]
        then
            cp ${sample}.total.fusion.xls ${sample}/${sample}.genefuse.xls.redup.xls && \
            cp ${sample}.var.${ref_version}_multianno*.xls ${sample} && \
            cp ${sample}.panel_cnv.zip ${sample} && \
            cp ${sample}.qc_stat.xls ${sample} && \
            cp ${sample}.qc_stat_dedup.xls ${sample} && \
            cp ${bamdst_report} ${sample} && \
            cp ${bamdst_report_dedup} ${sample} && \
            cp ${sample}.chemo.estimate.xls ${sample} && \
            cp ${sv_vcf} ${sample} && \
            cp ${sample}.sv.Rec.xls ${sample} && \
            cp ${flt3_vcf} ${sample} && \
            cp ${flt3_summary} ${sample}
        else
            cp ${genefuse} ${sample} && \
            cp ${fusion_html} ${sample} && \
            cp ${sample}.total.fusion.xls ${sample}/${sample}.genefuse.xls.redup.xls && \
            cp ${sample}.var.${ref_version}_multianno*.xls ${sample} && \
            cp ${sample}.backbone_cnv.zip ${sample} && \
            cp ${sample}.panel_cnv.zip ${sample} && \
            cp ${sample}.qc_stat.xls ${sample} && \
            cp ${sample}.qc_stat_dedup.xls ${sample} && \
            cp ${bamdst_report} ${sample} && \
            cp ${bamdst_report_dedup} ${sample} && \
            cp ${sample}.hla.tsv ${sample} && \
            cp ${sample}.chemo.estimate.xls ${sample} && \
            cp ${sv_vcf} ${sample} && \
            cp ${sample}.sv.Rec.xls ${sample} && \
            cp ${flt3_vcf} ${sample} && \
            cp ${flt3_summary} ${sample}
        fi

        python /scripts/create_task.py -s ${sample} -i ${sample}.sample.info.txt -n ${sample}.sample.info.new.txt \
        -ii ${sample}.sample.info.list.txt -nl ${sample}.sample.info.list.new.txt -o ${output_dir} && \
        mv ${sample} ${sample}-${sample_name} && \
        zip -r ${sample}-${sample_name}.zip ${sample}-${sample_name} -x *.bam
    }
    output {
        File anno_filter_germline_xls = "${sample}.var.${ref_version}_multianno.filter.germline.xls"
        File anno_germline_xls = "${sample}.var.${ref_version}_multianno.all.germline.xls"
        File anno_filter_xls = "${sample}.var.${ref_version}_multianno.filter.xls"
        File anno_xls = "${sample}.var.${ref_version}_multianno.all.xls"
        File collect_sample_info = "${sample}.sample.info.txt"
        File filter_gene_fusion = "${sample}.genefuse.xls.redup.xls"
        File sv_fusion = "${sample}.sv.xls"
        File total_fusion = "${sample}.total.fusion.xls"
        File qc_stat = "${sample}.qc_stat.xls"
        File qc_stat_dedup = "${sample}.qc_stat_dedup.xls"
        File chemo_xls = "${sample}.chemo.estimate.xls"
        File? all_hla_tsv = "${sample}.hla.tsv"
        File cnv_zip_backbone = "${sample}.backbone_cnv.zip"
        File cnv_zip_panel = "${sample}.panel_cnv.zip"
        File sample_task_json = "${sample}.task.json"
        File sample_zip = "${sample}-${sample_name}.zip"
        File import_zip = "${sample}.import.zip"
    }
    runtime {
        docker: docker
        cpu: cpu
        memory: memory
        disk: disk
        env: {
            "BUCKET": "oss-zhenyuan-db"
        }
    }
}