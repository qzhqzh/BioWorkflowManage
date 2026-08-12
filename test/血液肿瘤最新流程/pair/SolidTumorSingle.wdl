version 1.0

workflow SolidTumorSingle {
    input {
        String sample
        String sample_type
        String ref_version = "hg19"
        File fastq1
        File fastq2
        File bed
        File gene_bed
        File interval_list
        Array[String] genomes =  [
            "/easygene-share/kszy/hg19/database/hg19.simp.fa",
            "/easygene-share/kszy/hg19/database/hg19.simp.fa.amb",
            "/easygene-share/kszy/hg19/database/hg19.simp.fa.ann",
            "/easygene-share/kszy/hg19/database/hg19.simp.fa.bwt",
            "/easygene-share/kszy/hg19/database/hg19.simp.fa.pac",
            "/easygene-share/kszy/hg19/database/hg19.simp.fa.sa",
            "/easygene-share/kszy/hg19/database/hg19.simp.fa.fai",
            "/easygene-share/kszy/hg19/database/hg19.simp.2bit",
        ]
        String humandb = "/easygene-share/kszy/hg19/humandb/"
        String sample_info
        String output_dir
    }

    call QC {
        input:
        sample = sample,
        fastq = (fastq1, fastq2),
    }

    call Align {
        input:
        sample = sample,
        genomes = genomes,
        cleaned_fastq = QC.cleaned_fastq,
    }

    call CallFusionFactera {
        input:
        sample = sample,
        genomes = genomes,
        align_bam = Align.align_bam,
    }

    call CollectHsMetrics {
        input:
        sample = sample,
        genomes = genomes,
        bed = bed,
        align_bam = Align.align_bam,
    }

    call DeDup {
        input:
        sample = sample,
        genomes = genomes,
        bed = bed,
        align_bam = Align.align_bam
    }

    call CallSNV {
        input:
        sample = sample,
        genomes = genomes,
        bed = bed,
        dedup_bam = DeDup.dedup_bam,
    }

    call AnnoSNV {
        input:
        sample = sample,
        ref_version = ref_version,
        humandb =humandb,
        vcf = CallSNV.snv,
    }

    call CallCNV {
        input:
        sample = sample,
        dedup_bam = DeDup.dedup_bam,
    }

    call CallFusion {
        input:
        sample = sample,
        genomes = genomes,
        cleaned_fastq = QC.cleaned_fastq,
    }

    call CallChemo {
        input:
        sample = sample,
        anno_vcf = AnnoSNV.anno_vcf,
    }

    call ChemoQC {
        input:
        sample = sample,
        genomes = genomes,
        dedup_bam = DeDup.dedup_bam
    }

    call Msi {
        input:
        sample = sample,
        dedup_bam = DeDup.dedup_bam,
        bed = bed,
    }


    call Collect {
        input:
        sample = sample,
        sample_info = sample_info,
        ref_version = ref_version,
        sample_type = sample_type,
        genomes = genomes,
        anno_vcf = AnnoSNV.anno_vcf,
        cnv = CallCNV.cnv,
        genefuse = CallFusion.fusion_text,
        fusion_html = CallFusion.fusion_html,
        align_bam = Align.align_bam,
        output_dir = output_dir,
        chemo_tab1 = CallChemo.tab1,
        chemo_tab2 = CallChemo.tab2,
        chemo_estimate = CallChemo.estimate,
        bam_readcount = ChemoQC.bam_readcount,
        fastp_json = QC.fastp_json,
        fastp_html = QC.fastp_html,
        hs_metrics = CollectHsMetrics.hs_metrics,
        coverage = CollectHsMetrics.coverage,
        bamdst_report = CollectHsMetrics.bamdst_report,
        ctDNA_msi = Msi.ctDNA_msi,
        msi = Msi.msi,
        msi_pro = Msi.msi_pro,
        msi2 = Msi.msi2
    }

    output {
        File qc_json = QC.fastp_json
        File qc_html = QC.fastp_html
        Pair[File, File] dedup_bam =  DeDup.dedup_bam
        File coverage = CollectHsMetrics.coverage
        File hs_metrics = CollectHsMetrics.hs_metrics
        File bamdst_report = CollectHsMetrics.bamdst_report
        File snv = CallSNV.snv
        File anno_vcf = AnnoSNV.anno_vcf
        File cns = CallCNV.cns
        File cnv = CallCNV.cnv
        File fusion_html = CallFusion.fusion_html
        File fusion_json = CallFusion.fusion_json
        File fusion_text = CallFusion.fusion_text
        File chemo_tab1 = CallChemo.tab1
        File chemo_tab2 = CallChemo.tab2
        File chemo_estimate = CallChemo.estimate
        File chemo_qc = Collect.chemo_qc
        File anno_filter_germline_xls = Collect.anno_filter_germline_xls
        File anno_germline_xls = Collect.anno_germline_xls
        File anno_filter_xls = Collect.anno_filter_xls
        File anno_xls = Collect.anno_xls
        File collect_sample_info = Collect.collect_sample_info
        File cnv_filter_xls = Collect.cnv_filter_xls
        File filter_gene_fusion = Collect.filter_gene_fusion
        File qc_stat = Collect.qc_stat
        File sample_task_json = Collect.sample_task_json
        File ctDNA_msi = Msi.ctDNA_msi
        File msi = Msi.msi
        File msi_pro = Msi.msi_pro
        File msi2 = Msi.msi2
        File sample_zip = Collect.sample_zip
    }
}


task QC {
    input {
        String docker = 'registry.cn-shanghai.aliyuncs.com/kszy-biosoft/fastp:v0.23.1'
        String sample
        Pair[File, File] fastq
        String fastp_opts = " --cut_tail --correction "
        String fastp_parma = " -f 1 "
        String fastp_parma2 = " -U --umi_loc per_read --umi_len 3 --umi_prefix UMI --umi_skip 3 "
        String umi = "UMI"
        Int cpu = 12
        String memory = "48G"
        String disk = "200G"
    }
    command {
        set -vex
        if [[ "${umi}" == "UMI" ]]
        then
            fastp \
            -i ${fastq.left} \
            -I ${fastq.right} \
            -o ${sample}.cleaned.step1.r1.fq.gz \
            -O ${sample}.cleaned.step1.r2.fq.gz \
            ${fastp_parma} \
            ${fastp_opts} \
            -w ${cpu} \
            -j ${sample}.fastp.json \
            -h ${sample}.fastp.html && \
            fastp \
            -i ${sample}.cleaned.step1.r1.fq.gz \
            -I ${sample}.cleaned.step1.r2.fq.gz \
            -o ${sample}.cleaned.r1.fq.gz \
            -O ${sample}.cleaned.r2.fq.gz \
            ${fastp_parma2} \
            ${fastp_opts} \
            -w ${cpu} \
            -j ${sample}.fastp.json \
            -h ${sample}.fastp.html
            rm ${sample}.cleaned.step1.r1.fq.gz
            rm ${sample}.cleaned.step1.r2.fq.gz
            fastp \
            -i ${fastq.left} \
            -I ${fastq.right} \
            -o ${sample}.cleaned.genefuse.r1.fq.gz \
            -O ${sample}.cleaned.genefuse.r2.fq.gz \
            -f 7 \
            -F 7 \
            --cut_right \
            --correction  \
            -w ${cpu} \
            -j ${sample}_genefuse.fastp.json \
            -h ${sample}_genefuse.fastp.html
        else
            fastp \
            -i ${fastq.left} \
            -I ${fastq.right} \
            -o ${sample}.cleaned.r1.fq.gz \
            -O ${sample}.cleaned.r2.fq.gz \
            -f 7 \
            -F 7 \
            --cut_right \
            --correction  \
            -w ${cpu} \
            -j ${sample}.fastp.json \
            -h ${sample}.fastp.html
            cp ${sample}.cleaned.r1.fq.gz ${sample}.cleaned.genefuse.r1.fq.gz
            cp ${sample}.cleaned.r2.fq.gz ${sample}.cleaned.genefuse.r2.fq.gz
        fi
    }
    output {
        Pair[File, File] cleaned_fastq = ("${sample}.cleaned.r1.fq.gz", "${sample}.cleaned.r2.fq.gz")
        Pair[File, File] cleaned_fastq_genefuse = ("${sample}.cleaned.genefuse.r1.fq.gz", "${sample}.cleaned.genefuse.r2.fq.gz")
        File fastp_json = "${sample}.fastp.json"
        File fastp_html = "${sample}.fastp.html"
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

task Align {
    input {
        String docker = 'registry.cn-shanghai.aliyuncs.com/kszy-biosoft/bwa:v0.7.17'
        String sample
        Int cpu = 16
        String memory = "32G"
        String disk = "200G"
        Pair[File, File] cleaned_fastq
        Array[String] genomes

    }
    command {
        set -vex
        bwa mem -M -Y -R "@RG\tID:${sample}\tSM:${sample}\tLB:lib" \
        -t ${cpu} \
        ${genomes[0]} \
        ${cleaned_fastq.left} \
        ${cleaned_fastq.right} | \
        samtools view -Sbh - | samtools sort -@ ${cpu} - -o ${sample}.sorted.bam && \
        samtools index ${sample}.sorted.bam
    }
    output {
        Pair[File, File] align_bam = ("${sample}.sorted.bam", "${sample}.sorted.bam.bai")
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

task CallFusionFactera {
    input {
        String docker = 'registry.cn-shanghai.aliyuncs.com/kszy-biosoft/factera:v1'
        String sample
        Int cpu = 4
        String memory = "32G"
        String disk = "200G"
        Pair[File, File] align_bam
        Array[String] genomes
        File exon = "oss://oss-zhenyuan-db/hg38/blood_tumor/bed/exons_hg38.bed"

    }
    command {
        set -vex
        mkdir ${sample} && \
        perl /scripts/factera.pl -o ${sample} ${align_bam.left} ${exon} ${genomes[7]} && \
        cp ${sample}/*.fusions.txt ${sample}.factera.fusions.txt && \
        cut -f1-17 ${sample}/*.fusions.txt >${sample}.factera.fusions.xls
    }
    output {
        File factera_fusions_xls = "${sample}.factera.fusions.xls"
        File factera_fusions_txt = "${sample}.factera.fusions.txt"
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

task DeDup {
    input {
        String docker = 'registry.cn-shanghai.aliyuncs.com/kszy-biosoft/gencore:v0.7.2'
        String sample
        Int cpu = 4
        String memory = "32G"
        String disk = "200G"
        Pair[File, File] align_bam
        Array[String] genomes
        File bed
    }
    command {
        set -vex
        gencore \
        -i ${align_bam.left} \
        -o ${sample}.unsort.dedup.bam \
        -b ${bed} \
        -r ${genomes[0]} \
        -u UMI && \
        samtools sort -@ ${cpu} -l 5 -o ${sample}.dedup.bam ${sample}.unsort.dedup.bam && \
        samtools index ${sample}.dedup.bam
    }
    output {
        Pair[File, File] dedup_bam = ("${sample}.dedup.bam", "${sample}.dedup.bam.bai")
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

task CollectHsMetrics {
    input {
        String docker = 'registry.cn-shanghai.aliyuncs.com/kszy-biosoft/qc_stat:2.18.1'
        String sample
        Int cpu = 2
        String memory = "8G"
        String disk = "100G"
        Pair[File, File] align_bam
        Array[String] genomes
        File bed
    }
    command {
        set -vex
        mkdir ${sample}_bamdst_res && \
        /bamdst/bamdst -p ${bed} -o ${sample}_bamdst_res ${align_bam.left} && \
        mv ${sample}_bamdst_res/coverage.report ${sample}_coverage.report && \
        mv ${sample}_bamdst_res/depth.tsv.gz ${sample}_depth.tsv.gz && \
        gunzip ${sample}_depth.tsv.gz
    }

    output {
        File hs_metrics = "${sample}_coverage.report"
        File coverage = "${sample}_depth.tsv"
        File bamdst_report = "${sample}_coverage.report"
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


task CallSNV {
    input {
        String docker = 'registry.cn-shanghai.aliyuncs.com/kszy-biosoft/vardict:v1.0'
        String sample
        Int cpu = 4
        String memory = "16G"
        String disk = "200G"
        Pair[File, File] dedup_bam
        Array[String] genomes
        File bed
        File genome_bed
        Boolean is_MRD
        String vardict_param = " -X 0 -f 0.001 -c 1 -S 2 -E 3 -th 4 "
        String filter_param = " -A -E -f 0.001 "
    }
    command {
        set -vex
        VarDict ${vardict_param} -G ${genomes[0]} -N ${sample} -b ${dedup_bam.left} ${if (is_MRD) then genome_bed else bed} | \
        teststrandbias.R | \
        var2vcf_valid.pl -N ${sample} ${filter_param} - > ${sample}.vcf
    }

    output {
        File snv = "${sample}.vcf"
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

task AnnoSNV {
    input {
        String docker = 'registry.cn-shanghai.aliyuncs.com/kszy-biosoft/annovar:v20180416_2'
        Int cpu = 2
        String memory = "16G"
        String disk = "300G"

        String sample
        String ref_version

        File vcf
        String humandb

    }
    command {
        set -vex
        perl /home/TOOLS/tools/annovar/current/bin/table_annovar.pl ${vcf} ${humandb} -buildver ${ref_version} -out ${sample}.var -remove \
        -protocol refGeneWithVer,cytoBand,simple_repeat,rmsk,genomicSuperDups,clinvar_20250915,1000g2015aug_all,avsnp150,cosmic70,dbnsfp42a,exac03,gnomad312_genome,mbiobank_ChinaMAP,cosmic_blood \
        -operation g,r,r,r,r,f,f,f,f,f,f,f,f,f \
        -nastring . -vcfinput --polish --argument '-hgvs -splicing_threshold 10',,,'--colsWanted 10#11',,,,,,,,,,
    }
    output {
        File anno_vcf = "${sample}.var.${ref_version}_multianno.vcf"
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


task CallCNV {
    input{
        String docker = 'registry.cn-shanghai.aliyuncs.com/kszy-biosoft/cnvkit:v0.9.5'
        Int cpu = 2
        String memory = "4G"
        String disk = "100G"
        String sample
        Pair[File, File] dedup_bam
        String cnv_opts = " --drop-low-coverage "
        File baseline
    }
    command {
        set -vex
        cnvkit batch ${dedup_bam.left} -r ${baseline} --output-dir ${sample}.cnvout ${cnv_opts} && \
        cp ${sample}.cnvout/${sample}.dedup.cns ${sample}.cns && \
        cnvkit call ${sample}.cns -o ${sample}.cnv.tsv
    }

    output {
        File cns = "${sample}.cns"
        File cnv = "${sample}.cnv.tsv"
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


task CallFusion {
    input {
        String docker = 'registry.cn-shanghai.aliyuncs.com/kszy-biosoft/genefuse:latest'
        Int cpu = 16
        Int small_cpu = 8
        String memory = "32G"
        String disk = "200G"
        String sample
        Array[String] genomes
        Pair[File, File] cleaned_fastq
        File druggable_region = "oss://oss-zhenyuan-db/hg38/blood_tumor/resource/druggable.hg38.csv"

    }
    Float cleaned_fastq_size = size(cleaned_fastq.left,"GB")
    command {
        set -vex
        python3 /scripts/timeout_genefuse.py \
        -r ${genomes[0]} \
        -d ${druggable_region} \
        -r1 ${cleaned_fastq.left} \
        -r2 ${cleaned_fastq.right} \
        -m ${sample}.genefuse.html \
        -j ${sample}.genefuse.json \
        -f ${sample}.genefuse.xls \
        -t ${if (cleaned_fastq_size < 3) then small_cpu else cpu}
    }
    output {
        File fusion_html = "${sample}.genefuse.html"
        File fusion_json = "${sample}.genefuse.json"
        File fusion_text = "${sample}.genefuse.xls"
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


task CallChemo {
    input {
        String docker = 'registry.cn-shanghai.aliyuncs.com/kszy-biosoft/chemo:1.0'
        Int cpu = 1
        String memory = "2G"
        String sample
        File anno_vcf
        File file_chemo_rs_uniq = "oss://oss-zhenyuan-db/hg19/resource/chemo.rs.uniq.120.in"
        File file_chemo_tab2_example = "oss://oss-zhenyuan-db/hg19/resource/chemo.tab2.example"
        File db_chemo_efficacy_toxicity = "oss://oss-zhenyuan-db/hg19/resource/chemo_efficacy_toxicity_database.txt"
        File file_chemo_tab1_example = "oss://oss-zhenyuan-db/hg19/resource/chemo.tab1.example"
    }
    command {
        set -vex
        python3 /scripts/get_chemo_rs_genotype.py ${anno_vcf} ${file_chemo_rs_uniq} ${file_chemo_tab2_example} ${sample}.chemo.tab2.xls && \
        python3 /scripts/get_chemo_effi_toxi.py ${sample}.chemo.tab2.xls ${db_chemo_efficacy_toxicity} ${file_chemo_tab1_example} ${sample}.chemo.tab1.xls ${sample}.chemo.estimate.xls
    }
    output {
        File tab1 = "${sample}.chemo.tab1.xls"
        File tab2 = "${sample}.chemo.tab2.xls"
        File estimate = "${sample}.chemo.estimate.xls"
    }
    runtime {
        docker: docker
        cpu: cpu
        memory: memory
        env: {
            "BUCKET": "oss-zhenyuan-db"
        }
    }
}

task Msi {
    input {
        String docker = 'registry.cn-shanghai.aliyuncs.com/kszy-biosoft/msi:v1'
        Int cpu = 2
        String memory = "2G"
        String sample
        Pair[File, File] dedup_bam
        File bed
        File microsatellites = 'oss://oss-zhenyuan-db/hg19/resource/msidb/microsatellites.list'
        File msi_pro_reference_baseline = 'oss://oss-zhenyuan-db/hg19/resource/msidb/reference.list_baseline'
    }
    command {
        set -vex
        /msisensor2/msisensor2 msi -M /msisensor2/models_hg19_GRCh37/ -t ${dedup_bam.left} -e ${bed} -b 8 -o ${sample}.tumor.msi && \
        /msisensor-pro/binary/msisensor-pro pro -d ${msi_pro_reference_baseline} -t ${dedup_bam.left} -o ${sample}.pro.tumor.msi && \
        /msisensor/msisensor msi -d ${microsatellites} -t ${dedup_bam.left} -e ${bed} -o ${sample}.msisensor.tumor.msi && \
        /msisensor-ct/msisensor-ct msi -D -M /msisensor-ct/models_hg19_GRCh37/ -t ${dedup_bam.left} -o ${sample}.ctDNA.tumor.msi
    }
    output {
        File ctDNA_msi = "${sample}.ctDNA.tumor.msi"
        File msi = "${sample}.msisensor.tumor.msi"
        File msi_pro = "${sample}.pro.tumor.msi"
        File msi2 = "${sample}.tumor.msi"
    }
    runtime {
        docker: docker
        cpu: cpu
        memory: memory
        env: {
            "BUCKET": "oss-zhenyuan-db"
        }
    }
}

task ChemoQC {
    input {
        String docker = 'registry.cn-shanghai.aliyuncs.com/kszy-biosoft/bam-readcount:v1'
        Int cpu = 2
        String memory = "4G"
        String sample
        Array[String] genomes
        Pair[File, File] dedup_bam
        File chemo_site_bed = "oss://oss-zhenyuan-db/hg19/resource/chemo_site.bed"
        String chemo_qc_param = 'BEGIN{FS=OFS="\t"}{split($6,A,":");split($7,C,":");split($8,G,":");split($9,T,":");split($10,N,":");print $1,$2,$3,$4,$5,"A:"A[2],"C:"C[2],"G:"G[2],"T:"T[2],"N:"N[2]}'

    }
    command {
        set -vex
        bam-readcount -w 1 -f ${genomes[0]} ${dedup_bam.left} -l ${chemo_site_bed} |awk '${chemo_qc_param} ' > ${sample}.bam.readcount.tsv
    }
    output {
        File bam_readcount = "${sample}.bam.readcount.tsv"
    }
    runtime {
        docker: docker
        cpu: cpu
        memory: memory
        env: {
            "BUCKET": "oss-zhenyuan-db"
        }
    }
}

task Collect {
    input {
        String docker = 'registry.cn-shanghai.aliyuncs.com/kszy-biosoft/collect:v1'
        Int cpu = 4
        String memory = "32G"
        String sample
        String sample_name
        String ref_version
        String sample_type
        Array[String] genomes
        File anno_vcf
        File cnv
        File genefuse
        File fusion_html
        Pair[File, File] align_bam
        String copy_number_gt_threshold = "5"
        File key_site = "oss://oss-zhenyuan-db/hg19/resource/combine.tsv"
        File gene_list
        File gene_transcript_matchup = "oss://oss-zhenyuan-db/hg19/resource/sorted.gene.tx.txt"
        File hotspot_gene = "oss://oss-zhenyuan-db/hg19/resource/hotspot_gene-20230227.xls"
        File tumor_gene = "oss://oss-zhenyuan-db/hg19/resource/tumor-gene-20230216.xlsx"
        File refgene = "oss://oss-zhenyuan-db/hg19/humandb/hg19_refGeneWithVer.txt"
        File refgff = "oss://oss-zhenyuan-db/hg19/database/genomic.gene.gff"
        File chemo_site_bed = "oss://oss-zhenyuan-db/hg19/resource/chemo_site.bed"
        String sample_info
        String output_dir
        File chemo_tab1
        File chemo_tab2
        File chemo_estimate
        File bam_readcount
        File fastp_json
        File fastp_html
        File hs_metrics
        File coverage
        File bamdst_report
        File ctDNA_msi
        File msi
        File msi_pro
        File msi2
    }
    command {
        set -vex
        echo ${sample_info} | base64 -d > ${sample}.sample.info.txt && \
        python3 /scripts/pre_select_v2.py -k ${key_site} -l ${gene_list} -s ${sample_type} -t ${gene_transcript_matchup} \
        -f ${anno_vcf} -o ${sample}.var.${ref_version}_multianno.filter.xls -g ${hotspot_gene}  -c ${tumor_gene} -r ${refgene} -r2 ${refgff} && \
        python3 /scripts/cnv_filter.py -g ${gene_list} -k ${key_site} -c ${copy_number_gt_threshold} -f ${cnv} -o ${sample}.cnv.filtered.xls && \
        python3 /scripts/process_genefuse_res.py -g ${gene_list} -b ${align_bam.left} -f ${genefuse} && \
        perl /scripts/sequencing_uniformity.pl ${coverage} > ${sample}.stat && \
        python /scripts/qc_statistical_result.py ${fastp_json} ${hs_metrics} ${sample}.stat ${bamdst_report} > ${sample}.qc_stat.xls && \
        python /scripts/chemo_qc.py -ref ${genomes[0]} -b ${chemo_site_bed} -v ${anno_vcf} -c ${bam_readcount} -o ${sample}.chemo.qc.xls && \
        mkdir ${sample} && \
        cp ${chemo_tab1} ${sample} && \
        cp ${chemo_tab2} ${sample} && \
        cp ${chemo_estimate} ${sample} && \
        cp ${sample}.chemo.qc.xls ${sample} && \
        cp ${genefuse} ${sample} && \
        cp ${fusion_html} ${sample} && \
        cp ${sample}.genefuse.xls.redup.xls ${sample} && \
        cp ${sample}.var.${ref_version}_multianno*.xls ${sample} && \
        cp ${cnv} ${sample} && \
        cp ${sample}.cnv.filtered.xls ${sample} && \
        cp ${sample}.qc_stat.xls ${sample} && \
        cp ${ctDNA_msi} ${sample} && \
        cp ${msi} ${sample} && \
        cp ${msi_pro} ${sample} && \
        cp ${msi2} ${sample} && \
        python /scripts/create_task.py -s ${sample} -i ${sample}.sample.info.txt -o ${output_dir} && \
        mv ${sample} ${sample}-${sample_name} && \
        zip -r ${sample}-${sample_name}.zip ${sample}-${sample_name} -x *.vcf -x *.bam
    }
    output {
        File anno_filter_germline_xls = "${sample}.var.${ref_version}_multianno.filter.germline.xls"
        File anno_germline_xls = "${sample}.var.${ref_version}_multianno.all.germline.xls"
        File anno_filter_xls = "${sample}.var.${ref_version}_multianno.filter.xls"
        File anno_xls = "${sample}.var.${ref_version}_multianno.all.xls"
        File collect_sample_info = "${sample}.sample.info.txt"
        File cnv_filter_xls = "${sample}.cnv.filtered.xls"
        File filter_gene_fusion = "${sample}.genefuse.xls.redup.xls"
        File qc_stat = "${sample}.qc_stat.xls"
        File chemo_qc = "${sample}.chemo.qc.xls"
        File sample_task_json = "${sample}.task.json"
        File sample_zip = "${sample}-${sample_name}.zip"
        File import_zip = "${sample}.import.zip"
    }
    runtime {
        docker: docker
        cpu: cpu
        memory: memory
        env: {
            "BUCKET": "oss-zhenyuan-db"
        }
    }
}