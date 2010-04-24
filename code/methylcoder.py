"""
write the forward and reverse mappings for use by bowtie.
the code in this file originally benefited from a read of the
methodology in bsseeker.
"""

__version__ = "0.2.3"

from pyfasta import Fasta
import numpy as np
import bsddb
import sys
import os.path as op
import os
from subprocess import Popen
from calculate_methylation_points import calc_methylation
from cbowtie import _update_conversions
from fastqindex import FastQIndex, FastQEntry
import string
import glob
import datetime
np.seterr(divide="ignore")


def revcomp(s, _comp=string.maketrans('ATCG', 'TAGC')):
    return s.translate(_comp)[::-1]

CPU_COUNT = 4
try:
    import multiprocessing
    CPU_COUNT = multiprocessing.cpu_count()
except ImportError:
    import processing
    CPU_COUNT = processing.cpuCount()

def write_c2t(fasta_name):
    """
    given a fasta file and write 2 new files. given some.fasta:
        `some.forward.c2t.fasta` contains the same headers but all C's 
                                 converted to T
        `some.reverse.c2t.fasta` contains the reverse-complemented sequece
                                 with all C's converted to T.
    """
    d = op.join(op.dirname(fasta_name), "bowtie_index")
    if not op.exists(d): os.mkdir(d)

    p, ext = op.splitext(op.basename(fasta_name)) # some.fasta -> some, fasta
    revname = "%s/%s.reverse.c2t%s" % (d, p, ext)
    forname = "%s/%s.forward.c2t%s" % (d, p, ext)
    if op.exists(revname) and op.exists(forname): return forname, revname
    fasta = Fasta(fasta_name)

    reverse_fh = open(revname, 'w')
    forward_fh = open(forname, 'w')
    print >>sys.stderr, "writing: %s, %s" % (revname, forname)

    try:
        for header in fasta.iterkeys():
            seq = str(fasta[header]).upper()
            assert not ">" in seq
            print >>reverse_fh, ">%s" % header
            print >>forward_fh, ">%s" % header

            print >>reverse_fh, revcomp(seq).replace('C', 'T')
            print >>forward_fh, seq.replace('C', 'T')

        reverse_fh.close(); forward_fh.close()
    except:
        try: reverse_fh.close(); forward_fh.close()
        except: pass
        os.unlink(revname)
        os.unlink(forname)
        raise

    return forname, revname

def is_up_to_date_b(a, b):
    return op.exists(b) and os.stat(b).st_mtime >= os.stat(a).st_mtime


def run_bowtie_builder(bowtie_path, fasta_path):
    d = os.path.dirname(fasta_path)
    p, ext = op.splitext(op.basename(fasta_path)) # some.fasta -> some, fasta
    cmd = '%s/bowtie-build -f %s %s/%s | tee %s/%s.bowtie-build.log' % \
                (bowtie_path, fasta_path, d, p, d, p)

    if is_up_to_date_b(fasta_path, "%s/%s.1.ebwt" % (d, p)):
        return None
    print >>sys.stderr, "running: %s" % cmd
    process = Popen(cmd, shell=True)
    return process


def run_bowtie(opts, ref_path, reads_c2t, threads=CPU_COUNT, **kwargs):
    out_dir = opts.out_dir
    bowtie_path = opts.bowtie
    sam_out_file = out_dir + "/" + op.basename(ref_path) + ".sam"
    mismatches = opts.mismatches
    cmd = ("%(bowtie_path)s/bowtie --sam --sam-nohead " + \
           "--chunkmbs 1024 -v %(mismatches)d --norc " + \
           "--best -p %(threads)d %(ref_path)s -q %(reads_c2t)s") % locals()

    if opts.k > 0: cmd += " -k %i" % opts.k
    if opts.M > 0: cmd += " -M %i" % opts.M
    elif opts.m > 0: cmd += " -m %i" % opts.m

    cmd += " %(sam_out_file)s 2>&1 | tee %(out_dir)s/bowtie.log" % locals()
    print >>sys.stderr, cmd.replace("//", "/")

    if is_up_to_date_b(ref_path + ".1.ebwt", sam_out_file) and \
       is_up_to_date_b(reads_c2t, sam_out_file):
        print >>sys.stderr, "^ up to date, not running ^"
        return sam_out_file, None

    process = Popen(cmd, shell=True)
    return sam_out_file, process


# http://bowtie-bio.sourceforge.net/manual.shtml#sam-bowtie-output
def parse_sam(sam_aln_file, direction, chr_lengths, get_records):

    for sline in open(sam_aln_file):
        # comment.
        if sline[0] == "@": continue
        # it was excluded because of -M
        line = sline.split("\t")
        # no reported alignments.
        if line[1] == '4': continue 
        # extra found via -M
        if line[4] == '0': continue 
        assert line[1] == '0', line

        read_id = line[0]
        seqid = line[2]
        pos0 = int(line[3]) - 1
        converted_seq = line[9]

        # we want to include the orginal, non converted reads
        # in the output file to view the alignment.
        # read_id is the line in the file.
        #fh_raw_reads.seek((read_id * read_len) + read_id)
        #raw_seq = fh_raw_reads.read(read_len)
        raw_fastq, converted_fastq = get_records(read_id)
        read_len = len(converted_seq)
        raw_seq = raw_fastq.seq

        if direction == 'f':
            line[9] = raw_seq
        else:
            pos0 = chr_lengths[seqid] - pos0 - read_len
            line[3] = str(pos0 + 1)
            # since the read matched the flipped genome. we flip it here.
            line[9] = raw_seq = revcomp(raw_seq)
            # flip the quality as well.
            line[10] = line[10][::-1]
            line[1] = '16' # alignment on reverse strand.
            converted_seq = revcomp(converted_fastq.seq)

        # NM:i:2
        NM = [x for x in line[11:] if x[0] == 'N' and x[1] == 'M'][0].rstrip()
        nmiss = int(NM[-1])
        line[-1] = line[-1].rstrip()
        yield dict(
            read_id=read_id,
            seqid=line[2],
            pos0=pos0,
            mapq=line[4],
            nmiss=nmiss,
            read_sequence=converted_seq,
            raw_read=raw_seq,
        ), line, read_len

def bin_paths_from_fasta(original_fasta, out_dir='', pattern_only=False):
    """
    given the fasta, return the paths to the binary
    files that will be created
    """
    opath = op.splitext(op.basename(original_fasta))[0]
    if pattern_only:
        return ((out_dir + "/") if out_dir else "") + opath + ".%s.*.bin"

    paths = [ out_dir + "/" + opath + ".%s.c.bin",
              out_dir + "/" + opath + ".%s.t.bin",
              out_dir + "/" + opath + ".%s.methyltype.bin" ]
    if out_dir == '':
        return [p.lstrip("/") for p in paths]
    return paths

def get_raw_and_c2t(header, fqidx, fh_raw_reads, fh_c2t_reads):
    """
    since we're sharing the same index for the reads and the c2t,
    we take the header and return each record
    """
    fpos = fqidx.get_position(header)
    fh_raw_reads.seek(fpos)
    fh_c2t_reads.seek(fpos)
    return FastQEntry(fh_raw_reads), FastQEntry(fh_c2t_reads)

def count_conversions(original_fasta, direction, sam_file, raw_reads, out_dir,
                      allowed_mismatches):
    # direction is either 'f'orward or 'r'everse. if reverse, need to subtract
    # from length of chromsome.
    assert direction in 'fr'
    print >>sys.stderr, "tabulating %s methylation for %s" % \
            (direction, original_fasta)

    fqidx = FastQIndex(raw_reads + ".c2t")
    fa = Fasta(original_fasta)
    fh_raw_reads = open(raw_reads, 'r')
    fh_c2t_reads = open(raw_reads + ".c2t", 'r')

    def get_records(header):
        return get_raw_and_c2t(header, fqidx, fh_raw_reads, fh_c2t_reads)


    chr_lengths = dict((k, len(fa[k])) for k in fa.iterkeys())
    mode = 'a' if direction == 'r' else 'w'

    out_sam = open(out_dir + "/methylcoded.sam", mode)
 
    # get the 3 possible binary files for each chr
    fc, ft, fmethyltype = \
            bin_paths_from_fasta(original_fasta, out_dir)

    counts = {}
    for k in fa.iterkeys():
        # so this will be a dict of position => conv
        # here we add to fc and ft np.fromfile() from the forward,
        # and add to it in the reverse. otherwise, just overwriting
        # below.
        if direction == 'r':
            counts[k] = {'t': np.fromfile(ft % k, dtype=np.uint32),
                         'c': np.fromfile(fc % k, dtype=np.uint32)}
        else:
            counts[k] = {'t': np.zeros((len(fa[k]),), dtype=np.uint32),
                         # total reads in which this c changed to t 
                         'c': np.zeros((len(fa[k]),), dtype=np.uint32)}
        assert len(fa[k]) == len(counts[k]['t']) == len(counts[k]['c'])

    skipped = 0
    align_count = 0
    pairs = "CT" if direction == "f" else "GA" # 
    for p, sam_line, read_len in parse_sam(sam_file, direction, chr_lengths, get_records):
        # read_id is also the line number from the original file.
        read_id = p['read_id']
        pos0 = p['pos0']
        align_count += 1
        raw = p['raw_read']

        # the position is the line num * the read_id + read_id (where the +
        # is to account for the newlines.
        genomic_ref = str(fa[p['seqid']][pos0:pos0 + read_len])
        DEBUG = False
        if DEBUG:
            araw, ac2t = get_records(read_id)
            print >>sys.stderr, "f['%s'][%i:%i + %i]" % (p['seqid'], pos0, 
                                                         pos0, read_len)
            #fh_c2t_reads.seek((read_id * read_len) + read_id)
            print >>sys.stderr, "mismatches:", p['nmiss']
            print >>sys.stderr, "ref        :", genomic_ref
            if direction == 'r':
                print >>sys.stderr, "raw_read(r):", raw
                c2t = ac2t.seq
                c2t = revcomp(c2t)
                assert c2t == p['read_sequence']

            else:
                print >>sys.stderr, "raw_read(f):", raw
                c2t = ac2t.seq
                assert c2t == p['read_sequence']
            print >>sys.stderr, "c2t        :",  c2t, "\n"

        # have to keep the ref in forward here to get the correct bp
        # positions. look for CT when forward and GA when back.
        current_mismatches = p['nmiss']
        # we send in the current mismatches and allowed mismatches so that in
        # the case where a C in the ref seq has becomes a T in the align seq
        # (but wasnt calc'ed as a mismatch because we had converted C=>T. if
        # these errors cause the number of mismatches to exceed the number of
        # allowed mismatches, we dont include the stats for this read.
        remaining_mismatches = allowed_mismatches - current_mismatches
        this_skipped = _update_conversions(genomic_ref, raw, pos0, pairs,
                                       counts[p['seqid']]['c'], 
                                       counts[p['seqid']]['t'],
                                      remaining_mismatches, read_len)
        if this_skipped == 0:
            # only print the line to the sam file if we use it in our calcs.
            print >>out_sam, "\t".join(sam_line)
        skipped += this_skipped
        if DEBUG:
            raw_input("press any key...\n")

    print >>sys.stderr, "total alignments: %i" % align_count
    print >>sys.stderr, \
            "skipped %i alignments (%.3f%%) where read T mapped to genomic C" % \
                  (skipped, 100.0 * skipped / align_count)

    if direction == 'r':
        out = open(op.dirname(fmethyltype) + "/methyl-data-%s.txt" \
                    % datetime.date.today(), 'w')
        print >>out, make_header()
        print >>out, "#seqid\tmt\tbp\tc\tt"

    for seqid in sorted(counts.keys()):
        cs = counts[seqid]['c']
        ts = counts[seqid]['t']
        csum = float(cs.sum())
        tsum = float(ts.sum())
        mask = (cs + ts) > 0
        meth = (cs[mask].astype('f') / (cs[mask] + ts[mask]))
        print >>sys.stderr, "chr: %s, cs: %i, ts: %i, methylation: %.4f" \
                % (seqid, csum, tsum, meth.mean())

        cs.tofile(fc % seqid)
        ts.tofile(ft % seqid)

        if direction == 'r':
            file_pat = bin_paths_from_fasta(original_fasta, out_dir, 
                                            pattern_only=True)

            print >>sys.stderr, "#> writing:", file_pat % seqid

            seq = str(fa[seqid])
            mtype = calc_methylation(seq)
            mtype.tofile(fmethyltype % seqid)
            to_text_file(cs, ts, mtype, seqid, out)


def to_text_file(cs, ts, methylation_type, seqid, out=sys.stdout):
    """
    convert the numpy arrays to a file of format:
    seqid [TAB] methylation_type [TAB] bp(0) [TAB] cs [TAB] ts
    """
    idxs, = np.where(cs + ts)
    for bp, mt, c, t in np.column_stack((idxs, methylation_type[idxs],
                                           cs[idxs], ts[idxs])):
        print >>out, "\t".join(map(str, (seqid, mt, bp, c, t)))

def write_sam_commands(out_dir, fasta):
    fh_lens = open("%s/chr.lengths.txt" % out_dir, "w")
    for k in fasta.keys():
        print >>fh_lens, "%s\t%i" % (k, len(fasta[k]))
    fh_lens.close()
    out = open("%s/commands.sam.sh" % out_dir, "w")
    print >> out, """\
SAMTOOLS=/usr/local/src/samtools/samtools

$SAMTOOLS view -b -t %(odir)s/chr.lengths.txt %(odir)s/methylcoded.sam \
        -o %(odir)s/methylcoded.unsorted.bam
$SAMTOOLS sort %(odir)s/methylcoded.unsorted.bam %(odir)s/methylcoded # suffix added
$SAMTOOLS index %(odir)s/methylcoded.bam
# TO view:
# $SAMTOOLS tview %(odir)s/methylcoded.bam %(fapath)s
    """ % dict(odir=out_dir, fapath=fasta.fasta_name)
    out.close()

def convert_reads_c2t(reads_path):
    """
    assumes all capitals returns the new path and creates and index.
    """
    c2t = reads_path + ".c2t"
    idx = c2t + FastQIndex.ext

    if is_up_to_date_b(reads_path, c2t) and is_up_to_date_b(c2t, idx): 
        return c2t, FastQIndex(c2t)
    print >>sys.stderr, "converting C to T in %s" % (reads_path)

    try:
        out = open(c2t, 'wb')
        db = bsddb.btopen(idx, 'n', cachesize=32768*2, pgsize=512)

        fh_fq = open(reads_path)
        tell = fh_fq.tell
        next_line = fh_fq.readline
        while True:
            pos = tell()
            header = next_line().rstrip()
            if not header: break
            db[header[1:]] = str(pos)
            seq = next_line()

            out.write(header + '\n')
            out.write(seq.replace('C', 'T'))
            out.write(next_line())
            out.write(next_line())
        out.close()
        print >>sys.stderr, "opening index"
        db.close()
    except:
        os.unlink(c2t)
        os.unlink(idx)
        raise
    return c2t, FastQIndex(c2t)



def make_header():
    return """\
#created by: %s
#on: %s
#from: %s
#version: %s""" % (" ".join(sys.argv), datetime.date.today(), 
                   op.abspath("."), __version__)

if __name__ == "__main__":
    import optparse
    p = optparse.OptionParser(__doc__)

    p.add_option("--bowtie", dest="bowtie", help="path to bowtie directory")
    p.add_option("--reads", dest="reads", help="path to fastq reads file")
    p.add_option("--outdir", dest="out_dir", help="path to a directory in "
                 "which to write the files", default=None)

    p.add_option("--mismatches", dest="mismatches", default=2, type="int",
             help="number of mismatches allowed. sent to bowtie executable")
    p.add_option("--reference", dest="reference",
             help="path to reference fasta file to which to align reads")
    p.add_option("-k", dest="k", type='int', help="bowtie's -k parameter", default=1)
    p.add_option("-m", dest="m", type='int', help="bowtie's -m parameter", default=-1)
    p.add_option("-M", dest="M", type='int', help="bowtie's -M parameter", default=-1)

    opts, args = p.parse_args()

    if not (opts.reads and opts.bowtie):
        sys.exit(p.print_help())

    fasta = opts.reference
    if fasta is None:
        assert len(args) == 1, "must specify path to fasta file"
        fasta = args[0]
        assert os.path.exists(fasta), "fasta: %s does not exist" % fasta
    if glob.glob("%s/*.bin" % opts.out_dir):
        print >>sys.stderr, "PLEASE use an empty out directory or move "\
                "the existing .bin files from %s" % opts.out_dir
        sys.exit(1)

    fforward_c2t, freverse_c2t = write_c2t(fasta)
    pforward = run_bowtie_builder(opts.bowtie, fforward_c2t)
    preverse = run_bowtie_builder(opts.bowtie, freverse_c2t)
    if preverse is not None: preverse.wait()
    if pforward is not None: pforward.wait()

    raw_reads = opts.reads
    c2t_reads, c2t_index = convert_reads_c2t(raw_reads)  
    ref_forward = op.splitext(fforward_c2t)[0]
    ref_reverse = op.splitext(freverse_c2t)[0]
    try:

        forward_sam, fprocess = run_bowtie(opts, ref_forward, c2t_reads)
        # wait for forward process to finish. then calculate.
        fprocess and fprocess.wait()
        reverse_sam, rprocess = run_bowtie(opts, ref_reverse, c2t_reads)
        # start tabulating forward results.
        count_conversions(fasta, 'f', forward_sam, raw_reads, opts.out_dir,
                          opts.mismatches)
        # then wait for reverse process to finished. before tabulating.
        rprocess and rprocess.wait()
        count_conversions(fasta, 'r', reverse_sam, raw_reads, opts.out_dir,
                      opts.mismatches)
    except:
        files = bin_paths_from_fasta(fasta, opts.out_dir, pattern_only=True)
        for f in glob.glob(files):
            print >>sys.stderr, "deleting:", f
            try: os.unlink(f)
            except OSError: pass
        print >>sys.stderr, "ERROR: don't use .bin or text files"
        raise
    finally:
        cmd = open(opts.out_dir +"/cmd.ran", "w")
        print >>cmd, "#date:", str(datetime.date.today())
        print >>cmd, "#path:", op.abspath(".")
        print >>cmd, " ".join(sys.argv)
        write_sam_commands(opts.out_dir, Fasta(fasta))

    print >>sys.stderr, "SUCCESS"
