#!/usr/bin/env python
#
# Copyright (c) 2010-2016 Varnish Software
# All rights reserved.
#
# Author: Poul-Henning Kamp <phk@phk.freebsd.dk>
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
# 1. Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in the
#    documentation and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE AUTHOR AND CONTRIBUTORS ``AS IS'' AND
# ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED.  IN NO EVENT SHALL AUTHOR OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS
# OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION)
# HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
# LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY
# OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF
# SUCH DAMAGE.

"""
Read the vmod.vcc file (inputvcc) and produce:
    vmod_if.h -- Prototypes for the implementation
    vmod_if.c -- Magic glue & datastructures to make things a VMOD.
    vmod_${name}.rst -- Extracted documentation
"""

# This script should work with both Python 2 and Python 3.
from __future__ import print_function

import os
import sys
import re
import optparse
import unittest
import random
import copy
import json

strict_abi = True

AMBOILERPLATE = '''
# Boilerplate generated by vmodtool.py - changes will be overwritten

AM_LDFLAGS  = $(AM_LT_LDFLAGS)

AM_CPPFLAGS = \\
\t-I$(top_srcdir)/include \\
\t-I$(top_srcdir)/bin/varnishd \\
\t-I$(top_builddir)/include

vmoddir = $(pkglibdir)/vmods
vmodtool = $(top_srcdir)/lib/libvcc/vmodtool.py
vmodtoolargs = --strict --boilerplate

vmod_LTLIBRARIES = libvmod_XXX.la

libvmod_XXX_la_CFLAGS = \\
\t@SAN_CFLAGS@

libvmod_XXX_la_LDFLAGS = \\
\t$(AM_LDFLAGS) \\
\t$(VMOD_LDFLAGS) \\
\t@SAN_LDFLAGS@

nodist_libvmod_XXX_la_SOURCES = vcc_if.c vcc_if.h

$(libvmod_XXX_la_OBJECTS): vcc_if.h

vcc_if.h vmod_XXX.rst vmod_XXX.man.rst: vcc_if.c

vcc_if.c: $(vmodtool) $(srcdir)/vmod.vcc
\t@PYTHON@ $(vmodtool) $(vmodtoolargs) $(srcdir)/vmod.vcc

EXTRA_DIST = vmod.vcc automake_boilerplate.am

CLEANFILES = $(builddir)/vcc_if.c $(builddir)/vcc_if.h \\
\t$(builddir)/vmod_XXX.rst \\
\t$(builddir)/vmod_XXX.man.rst

'''

privs = {
    'PRIV_CALL':   "struct vmod_priv *",
    'PRIV_VCL':    "struct vmod_priv *",
    'PRIV_TASK':   "struct vmod_priv *",
    'PRIV_TOP':    "struct vmod_priv *",
}

ctypes = {
    'ACL':         "VCL_ACL",
    'BACKEND':     "VCL_BACKEND",
    'BLOB':        "VCL_BLOB",
    'BODY':        "VCL_BODY",
    'BOOL':        "VCL_BOOL",
    'BYTES':       "VCL_BYTES",
    'DURATION':    "VCL_DURATION",
    'ENUM':        "VCL_ENUM",
    'HEADER':      "VCL_HEADER",
    'HTTP':        "VCL_HTTP",
    'INT':         "VCL_INT",
    'IP':          "VCL_IP",
    'PROBE':       "VCL_PROBE",
    'REAL':        "VCL_REAL",
    'STEVEDORE':   "VCL_STEVEDORE",
    'STRANDS':     "VCL_STRANDS",
    'STRING':      "VCL_STRING",
    'STRING_LIST': "const char *, ...",
    'TIME':        "VCL_TIME",
    'VOID':        "VCL_VOID",
}

ctypes.update(privs)

#######################################################################


def write_file_warning(fo, a, b, c):
    fo.write(a + "\n")
    fo.write(b + " NB:  This file is machine generated, DO NOT EDIT!\n")
    fo.write(b + "\n")
    fo.write(b + " Edit vmod.vcc and run make instead\n")
    fo.write(c + "\n\n")


def write_c_file_warning(fo):
    write_file_warning(fo, "/*", " *", " */")


def write_rst_file_warning(fo):
    write_file_warning(fo, "..", "..", "..")


def write_rst_hdr(fo, s, below="-", above=None):
    if above is not None:
        fo.write(above * len(s) + "\n")
    fo.write(s + "\n")
    if below is not None:
        fo.write(below * len(s) + "\n")

#######################################################################


def lwrap(s, width=64):
    """
    Wrap a C-prototype like string into a number of lines.
    """
    ll = []
    p = ""
    while len(s) > width:
        y = s[:width].rfind(',')
        if y == -1:
            y = s[:width].rfind('(')
        if y == -1:
            break
        ll.append(p + s[:y + 1])
        s = s[y + 1:].lstrip()
        p = "    "
    if len(s) > 0:
        ll.append(p + s)
    return ll


def quote(s):
    return s.replace("\"", "\\\"")


def indent(p, n):
    n = len(p.expandtabs()) + n
    p = "\t" * int(n / 8)
    p += " " * int(n % 8)
    return p

#######################################################################


def err(str, warn=True):
    if opts.strict or not warn:
        print("ERROR: " + str, file=sys.stderr)
        exit(1)
    else:
        print("WARNING: " + str, file=sys.stderr)


def fmt_cstruct(fo, mn, x):
    a = "\ttd_" + mn + "_" + x
    while len(a.expandtabs()) < 40:
        a += "\t"
    fo.write("%s*%s;\n" % (a, x))

#######################################################################


enum_values = {}


class ctype(object):
    def __init__(self, vt, ct):
        self.vt = vt
        self.ct = ct
        self.nm = None
        self.defval = None
        self.spec = None

    def __str__(self):
        s = "<" + self.vt
        if self.nm is not None:
            s += " " + self.nm
        if self.defval is not None:
            s += " VAL=" + self.defval
        if self.spec is not None:
            s += " SPEC=" + str(self.spec)
        return s + ">"

    def vcl(self):
        if self.vt == "STRING_LIST":
            return "STRING"
        if self.spec is None:
            return self.vt
        return self.vt + " {" + ", ".join(self.spec) + "}"

    def synopsis(self):
        if self.vt == "STRING_LIST":
            return "STRING"
        return self.vt

    def json(self, jl):
        jl.append([self.vt, self.nm, self.defval, self.spec])
        while jl[-1][-1] is None:
                jl[-1].pop(-1)


def vtype(txt):
    j = len(txt)
    for i in (',', ' ', '\n', '\t'):
        x = txt.find(i)
        if x > 0:
            j = min(j, x)
    t = txt[:j]
    r = txt[j:].lstrip()
    if t not in ctypes:
        err("Did not recognize type <%s>" % txt)
    ct = ctype(t, ctypes[t])
    if t != "ENUM":
        return ct, r
    assert r[0] == '{'
    e = r[1:].split('}', 1)
    r = e[1].lstrip()
    e = e[0].split(',')
    ct.spec = []
    for i in e:
        j = i.strip()
        enum_values[j] = True
        ct.spec.append(j)
    return ct, r


def arg(txt):
    a, s = vtype(txt)
    if len(s) == 0 or s[0] == ',':
        return a, s

    i = s.find('=')
    j = s.find(',')
    if j < 0:
        j = len(s)
    if j < i:
        i = -1
    if i < 0:
        i = s.find(',')
        if i < 0:
            i = len(s)
        a.nm = s[:i].rstrip()
        s = s[i:]
        return a, s

    a.nm = s[:i].rstrip()
    s = s[i + 1:].lstrip()
    if s[0] == '"' or s[0] == "'":
        m = re.match("(['\"]).*?(\\1)", s)
        if not m:
            err("Unbalanced quote")
        a.defval = s[:m.end()]
        if a.vt == "ENUM":
            a.defval = a.defval[1:-1]
        s = s[m.end():]
    else:
        i = s.find(',')
        if i < 0:
            i = len(s)
        a.defval = s[:i].rstrip()
        s = s[i:]
    if a.vt == "ENUM" and a.defval not in a.spec:
        err("ENUM default value <%s> not valid" % a.defval, warn=False)

    return a, s


def nmlegal(nm):
    return re.match('^[a-zA-Z0-9_]+$', nm)


# XXX cant have ( or ) in an argument default value
class prototype(object):
    def __init__(self, st, retval=True, prefix=""):
        self.st = st
        self.obj = None
        ll = st.line[1]

        if retval:
            self.retval, s = vtype(ll)
        else:
            self.retval = None
            s = ll
        i = s.find("(")
        assert i > 0
        self.prefix = prefix
        self.bname = s[:i].strip()
        self.name = self.prefix + self.bname
        self.vcc = st.vcc
        if not nmlegal(self.cname()):
            err("%s(): Illegal name\n" % self.name, warn=False)
        s = s[i:].strip()
        assert s[0] == "("
        assert s[-1] == ")"
        s = s[1:-1].lstrip()
        self.args = []
        names = {}
        while len(s) > 0:
            a, s = arg(s)
            if a.nm is not None:
                if not nmlegal(a.nm):
                    err("%s(): illegal argument name '%s'\n"
                        % (self.name, a.nm), warn=False)
                if a.nm in names:
                    err("%s(): duplicate argument name '%s'\n"
                        % (self.name, a.nm), warn=False)
                names[a.nm] = True
            self.args.append(a)
            s = s.lstrip()
            if len(s) == 0:
                break
            assert s[0] == ','
            s = s[1:].lstrip()

    def cname(self, pfx=False):
        r = self.name.replace(".", "_")
        if pfx:
            return self.vcc.sympfx + r
        return r

    def vcl_proto(self, short, pfx=""):
        if type(self.st) == s_method:
            pfx += pfx
        s = pfx
        if type(self.st) == s_object:
            s += "new x" + self.bname + " = "
        elif self.retval is not None:
            s += self.retval.vcl() + " "

        if type(self.st) == s_method:
            s += self.obj + self.bname + "("
        else:
            s += self.name + "("
        ll = []
        for i in self.args:
            if short:
                t = i.synopsis()
            else:
                t = i.vcl()
            if t in privs:
                continue
            if not short:
                if i.nm is not None:
                    t += " " + i.nm
                if i.defval is not None:
                    t += "=" + i.defval
            ll.append(t)
        t = ",@".join(ll)
        if len(s + t) > 68 and not short:
            s += "\n" + pfx + pfx
            s += t.replace("@", "\n" + pfx + pfx)
            s += "\n" + pfx + ")"
        else:
            s += t.replace("@", " ") + ")"
        return s

    def rsthead(self, fo):
        s = self.vcl_proto(False)
        if len(s) < 60:
            write_rst_hdr(fo, s, '-')
        else:
            s = self.vcl_proto(True)
            if len(s) > 60:
                s = self.name + "(...)"
            write_rst_hdr(fo, s, '-')
            fo.write("\n::\n\n" + self.vcl_proto(False, pfx="   ") + "\n")

    def synopsis(self, fo, man):
        fo.write(self.vcl_proto(True, pfx="   ") + "\n")
        fo.write("  \n")

    def c_ret(self):
        return self.retval.ct

    def c_args(self, a=[]):
        ll = list(a)
        for i in self.args:
            ll.append(i.ct)
        return ", ".join(ll)

    def c_fn(self, args=[], h=False):
        s = fn = ''
        if not h:
            s += 'typedef '
            fn += 'td_' + self.vcc.modname + '_'
        fn += self.cname(pfx=h)
        s += '%s %s(%s);' % (self.c_ret(), fn, self.c_args(args))
        return "\n".join(lwrap(s)) + "\n"

    def json(self, jl, cfunc):
        ll = []
        self.retval.json(ll)
        ll.append(cfunc)
        for i in self.args:
            i.json(ll)
        jl.append(ll)

#######################################################################


class stanza(object):
    def __init__(self, l0, doc, vcc):
        self.line = l0
        while len(doc) > 0 and doc[0] == '':
            doc.pop(0)
        while len(doc) > 0 and doc[-1] == '':
            doc.pop(-1)
        self.doc = doc
        self.vcc = vcc
        self.rstlbl = None
        self.methods = None
        self.proto = None
        self.parse()

    def dump(self):
        print(type(self), self.line)

    def rstfile(self, fo, man):
        if self.rstlbl is not None:
            fo.write(".. _" + self.rstlbl + ":\n\n")

        self.rsthead(fo, man)
        fo.write("\n")
        self.rstmid(fo, man)
        fo.write("\n")
        self.rsttail(fo, man)
        fo.write("\n")

    def rsthead(self, fo, man):
        if self.proto is None:
            return
        self.proto.rsthead(fo)

    def rstmid(self, fo, man):
        fo.write("\n".join(self.doc) + "\n")

    def rsttail(self, fo, man):
        return

    def synopsis(self, fo, man):
        if self.proto is not None:
            self.proto.synopsis(fo, man)

    def hfile(self, fo):
        return

    def cstruct(self, fo):
        return

    def cstruct_init(self, fo):
        return

    def json(self, jl):
        return

#######################################################################


class s_module(stanza):
    def parse(self):
        a = self.line[1].split(None, 2)
        self.vcc.modname = a[0]
        self.vcc.mansection = a[1]
        self.vcc.moddesc = a[2]
        self.rstlbl = "vmod_%s(%s)" % (
            self.vcc.modname,
            self.vcc.mansection
        )
        self.vcc.contents.append(self)

    def rsthead(self, fo, man):

        write_rst_hdr(fo, self.vcc.sympfx + self.vcc.modname, "=", "=")
        fo.write("\n")

        write_rst_hdr(fo, self.vcc.moddesc, "-", "-")

        fo.write("\n")
        fo.write(":Manual section: " + self.vcc.mansection + "\n")

        fo.write("\n")
        write_rst_hdr(fo, "SYNOPSIS", "=")
        fo.write("\n")
        fo.write("\n::\n\n")
        fo.write('   import %s [from "path"] ;\n' % self.vcc.modname)
        fo.write("   \n")
        for c in self.vcc.contents:
            c.synopsis(fo, man)
        fo.write("\n")

    def rsttail(self, fo, man):

        if man:
            return

        write_rst_hdr(fo, "CONTENTS", "=")
        fo.write("\n")

        ll = []
        for i in self.vcc.contents[1:]:
            j = i.rstlbl
            if j is not None:
                ll.append([j.split("_", 1)[1], j])
            if i.methods is None:
                continue
            for x in i.methods:
                j = x.rstlbl
                ll.append([j.split("_", 1)[1], j])

        ll.sort()
        for i in ll:
            fo.write("* :ref:`%s`\n" % i[1])
        fo.write("\n")


class s_abi(stanza):
    def parse(self):
        global strict_abi
        if self.line[1] not in ('strict', 'vrt'):
            err("Valid ABI types are 'strict' or 'vrt', got '%s'\n" %
                self.line[1])
        strict_abi = self.line[1] == 'strict'
        self.vcc.contents.append(self)


class s_prefix(stanza):
    def parse(self):
        self.vcc.sympfx = self.line[1] + "_"
        self.vcc.contents.append(self)


class s_event(stanza):
    def parse(self):
        self.event_func = self.line[1]
        self.vcc.contents.append(self)

    def rstfile(self, fo, man):
        if len(self.doc) != 0:
            err("Not emitting .RST for $Event %s\n" %
                self.event_func)

    def hfile(self, fo):
        fo.write("vmod_event_f %s;\n" % self.event_func)

    def cstruct(self, fo):
        fo.write("\tvmod_event_f\t\t\t*_event;\n")

    def cstruct_init(self, fo):
        fo.write("\t%s,\n" % self.event_func)

    def json(self, jl):
        jl.append([
                "$EVENT",
                "Vmod_%s_Func._event" % self.vcc.modname
        ])


class s_function(stanza):
    def parse(self):
        self.proto = prototype(self)
        self.rstlbl = "func_" + self.proto.name
        self.vcc.contents.append(self)

    def hfile(self, fo):
        fo.write(self.proto.c_fn(['VRT_CTX'], True))

    def cfile(self, fo):
        fo.write(self.proto.c_fn(['VRT_CTX']))

    def cstruct(self, fo):
        fmt_cstruct(fo, self.vcc.modname, self.proto.cname())

    def cstruct_init(self, fo):
        fo.write("\t" + self.proto.cname(pfx=True) + ",\n")

    def json(self, jl):
        jl.append([
                "$FUNC",
                "%s" % self.proto.name,
        ])
        self.proto.json(jl[-1], 'Vmod_%s_Func.%s' %
                        (self.vcc.modname, self.proto.cname()))


class s_object(stanza):
    def parse(self):
        self.proto = prototype(self, retval=False)
        self.proto.retval = vtype('VOID')[0]
        self.proto.obj = "x" + self.proto.name

        self.init = copy.copy(self.proto)
        self.init.name += '__init'

        self.fini = copy.copy(self.proto)
        self.fini.name += '__fini'
        self.fini.args = []

        self.rstlbl = "obj_" + self.proto.name
        self.vcc.contents.append(self)
        self.methods = []

    def rsthead(self, fo, man):
        self.proto.rsthead(fo)

        fo.write("\n" + "\n".join(self.doc) + "\n\n")

        for i in self.methods:
            i.rstfile(fo, man)

    def rstmid(self, fo, man):
        return

    def synopsis(self, fo, man):
        self.proto.synopsis(fo, man)
        for i in self.methods:
            i.proto.synopsis(fo, man)

    def chfile(self, fo, h):
        sn = self.vcc.sympfx + self.vcc.modname + "_" + self.proto.name
        fo.write("struct %s;\n" % sn)

        fo.write(self.init.c_fn(
            ['VRT_CTX', 'struct %s **' % sn, 'const char *'], h))
        fo.write(self.fini.c_fn(['struct %s **' % sn], h))
        for i in self.methods:
            fo.write(i.proto.c_fn(['VRT_CTX', 'struct %s *' % sn], h))
        fo.write("\n")

    def hfile(self, fo):
        self.chfile(fo, True)

    def cfile(self, fo):
        self.chfile(fo, False)

    def cstruct(self, fo):
        fmt_cstruct(fo, self.vcc.modname, self.init.name)
        fmt_cstruct(fo, self.vcc.modname, self.fini.name)
        for i in self.methods:
            i.cstruct(fo)

    def cstruct_init(self, fo):
        p = "\t" + self.vcc.sympfx
        fo.write(p + self.init.name + ",\n")
        fo.write(p + self.fini.name + ",\n")
        for i in self.methods:
            i.cstruct_init(fo)
        fo.write("\n")

    def json(self, jl):
        ll = [
                "$OBJ",
                self.proto.name,
                "struct %s%s_%s" %
                (self.vcc.sympfx, self.vcc.modname, self.proto.name),
        ]

        l2 = ["$INIT"]
        ll.append(l2)
        self.init.json(l2,
                       'Vmod_%s_Func.%s' % (self.vcc.modname, self.init.name))

        l2 = ["$FINI"]
        ll.append(l2)
        self.fini.json(l2,
                       'Vmod_%s_Func.%s' % (self.vcc.modname, self.fini.name))

        for i in self.methods:
                i.json(ll)

        jl.append(ll)

    def dump(self):
        super(s_object, self).dump()
        for i in self.methods:
            i.dump()


class s_method(stanza):
    def parse(self):
        p = self.vcc.contents[-1]
        assert type(p) == s_object
        self.pfx = p.proto.name
        self.proto = prototype(self, prefix=self.pfx)
        self.proto.obj = "x" + self.pfx
        self.rstlbl = "func_" + self.proto.name
        p.methods.append(self)

    def cstruct(self, fo):
        fmt_cstruct(fo, self.vcc.modname, self.proto.cname())

    def cstruct_init(self, fo):
        fo.write('\t' + self.proto.cname(pfx=True) + ",\n")

    def json(self, jl):
        jl.append([
                "$METHOD",
                self.proto.name[len(self.pfx)+1:]
        ])
        self.proto.json(jl[-1],
                        'Vmod_%s_Func.%s' %
                        (self.vcc.modname, self.proto.cname()))


#######################################################################

dispatch = {
    "Module":   s_module,
    "Prefix":   s_prefix,
    "ABI":      s_abi,
    "Event":    s_event,
    "Function": s_function,
    "Object":   s_object,
    "Method":   s_method,
}


class vcc(object):
    def __init__(self, inputvcc, rstdir, outputprefix):
        self.inputfile = inputvcc
        self.rstdir = rstdir
        self.pfx = outputprefix
        self.sympfx = "vmod_"
        self.contents = []
        self.commit_files = []
        self.copyright = ""

    def openfile(self, fn):
        self.commit_files.append(fn)
        return open(fn + ".tmp", "w")

    def commit(self):
        for i in self.commit_files:
            os.rename(i + ".tmp", i)

    def parse(self):
        a = "\n" + open(self.inputfile, "r").read()
        s = a.split("\n$")
        self.copyright = s.pop(0).strip()
        while len(s):
            ss = s.pop(0)
            i = ss.find("\n\n")
            if i > -1:
                i += 1
            else:
                i = len(ss)
            c = ss[:i].split()
            m = dispatch.get(c[0])
            if m is None:
                err("Unknown stanze $%s" % ss[:i])
            m([c[0], " ".join(c[1:])], ss[i:].split('\n'), self)

    def rst_copyright(self, fo):
        write_rst_hdr(fo, "COPYRIGHT", "=")
        fo.write("\n::\n\n")
        a = self.copyright
        a = a.replace("\n#", "\n ")
        if a[:2] == "#\n":
            a = a[2:]
        if a[:3] == "#-\n":
            a = a[3:]
        fo.write(a + "\n")

    def rstfile(self, man=False):
        fn = os.path.join(self.rstdir, "vmod_" + self.modname)
        if man:
            fn += ".man"
        fn += ".rst"
        fo = self.openfile(fn)
        write_rst_file_warning(fo)
        fo.write(".. role:: ref(emphasis)\n\n")

        for i in self.contents:
            i.rstfile(fo, man)

        if len(self.copyright):
            self.rst_copyright(fo)

        fo.close()

    def amboilerplate(self):
        fn = "automake_boilerplate.am"
        fo = self.openfile(fn)
        fo.write(AMBOILERPLATE.replace("XXX", self.modname))
        fo.close()

    def hfile(self):
        fn = self.pfx + ".h"
        fo = self.openfile(fn)
        write_c_file_warning(fo)
        fo.write("#ifndef VDEF_H_INCLUDED\n")
        fo.write('#  error "Include vdef.h first"\n')
        fo.write("#endif\n")
        fo.write("#ifndef VRT_H_INCLUDED\n")
        fo.write('#  error "Include vrt.h first"\n')
        fo.write("#endif\n")
        fo.write("\n")

        for j in sorted(enum_values):
            fo.write("extern VCL_ENUM %senum_%s;\n" % (self.sympfx, j))
        fo.write("\n")

        for j in self.contents:
            j.hfile(fo)
        fo.close()

    def cstruct(self, fo, csn):

        fo.write("\n%s {\n" % csn)
        for j in self.contents:
            j.cstruct(fo)
        fo.write("\n")
        for j in sorted(enum_values):
            fo.write("\tVCL_ENUM\t\t\t*enum_%s;\n" % j)
        fo.write("};\n")

    def cstruct_init(self, fo, csn):
        fo.write("\nstatic const %s Vmod_Func = {\n" % csn)
        for j in self.contents:
            j.cstruct_init(fo)
        fo.write("\n")
        for j in sorted(enum_values):
            fo.write("\t&%senum_%s,\n" % (self.sympfx, j))
        fo.write("};\n")

    def json(self, fo):
        jl = [["$VMOD", "1.0"]]
        for j in self.contents:
                j.json(jl)

        bz = bytearray(json.dumps(jl, separators=(",", ":")),
                       encoding = "ascii") + b"\0"
        fo.write("\nstatic const char Vmod_Json[%d] = {\n" % len(bz))
        t = "\t"
        for i in bz:
                t += "%d," % i
                if len(t) >= 69:
                        fo.write(t + "\n")
                        t = "\t"
        if len(t) > 1:
                fo.write(t[:-1])
        fo.write("\n};\n\n")
        for i in json.dumps(jl, indent=2, separators=(',', ': ')).split("\n"):
                j = "// " + i
                if len(j) > 72:
                        fo.write(j[:72] + "[...]\n")
                else:
                        fo.write(j + "\n")
        fo.write("\n")

    def api(self, fo):
        for i in (714, 759, 765):
            fo.write("\n/*lint -esym(%d, Vmod_%s_Data) */\n" %
                     (i, self.modname))
        fo.write("\nextern const struct vmod_data Vmod_%s_Data;\n" %
                 (self.modname))
        fo.write("\nconst struct vmod_data Vmod_%s_Data = {\n" % self.modname)
        if strict_abi:
            fo.write("\t.vrt_major =\t0,\n")
            fo.write("\t.vrt_minor =\t0,\n")
        else:
            fo.write("\t.vrt_major =\tVRT_MAJOR_VERSION,\n")
            fo.write("\t.vrt_minor =\tVRT_MINOR_VERSION,\n")
        fo.write('\t.name =\t\t"%s",\n' % self.modname)
        fo.write('\t.func =\t\t&Vmod_Func,\n')
        fo.write('\t.func_len =\tsizeof(Vmod_Func),\n')
        fo.write('\t.proto =\tVmod_Proto,\n')
        fo.write('\t.json =\t\tVmod_Json,\n')
        fo.write('\t.abi =\t\tVMOD_ABI_Version,\n')
        # NB: Sort of hackish:
        # Fill file_id with random stuff, so we can tell if
        # VCC and VRT_Vmod_Init() dlopens the same file
        #
        fo.write("\t.file_id =\t\"")
        for i in range(32):
            fo.write("%c" % random.randint(0x40, 0x5a))
        fo.write("\",\n")
        fo.write("};\n")

    def cfile(self):
        fn = self.pfx + ".c"
        fo = self.openfile(fn)
        write_c_file_warning(fo)

        fn2 = fn + ".tmp2"

        fo.write('#include "config.h"\n')
        fo.write('#include <stdio.h>\n')
        for i in ["vdef", "vrt", self.pfx, "vmod_abi"]:
            fo.write('#include "%s.h"\n' % i)

        fo.write("\n")

        for j in sorted(enum_values):
            fo.write('VCL_ENUM %senum_%s = "%s";\n' % (self.sympfx, j, j))
        fo.write("\n")

        fx = open(fn2, "w")

        for i in self.contents:
            if type(i) == s_object:
                i.cfile(fo)
                i.cfile(fx)

        fx.write("/* Functions */\n")
        for i in self.contents:
            if type(i) == s_function:
                i.cfile(fo)
                i.cfile(fx)

        csn = "Vmod_%s_Func" % self.modname

        self.cstruct(fo, "struct " + csn)

        self.cstruct(fx, "struct " + csn)

        fo.write("\n/*lint -esym(754, Vmod_" + self.modname + "_Func::*) */\n")
        self.cstruct_init(fo, "struct " + csn)

        fx.close()

        fo.write("\nstatic const char Vmod_Proto[] =\n")
        fi = open(fn2)
        for i in fi:
            fo.write('\t"%s\\n"\n' % i.rstrip())
        fi.close()
        fo.write('\t"static struct %s %s;";\n' % (csn, csn))

        os.remove(fn2)

        self.json(fo)

        self.api(fo)

        fo.close()

#######################################################################


def runmain(inputvcc, rstdir, outputprefix):

    v = vcc(inputvcc, rstdir, outputprefix)
    v.parse()

    v.rstfile(man=False)
    v.rstfile(man=True)
    v.hfile()
    v.cfile()
    if opts.boilerplate:
        v.amboilerplate()

    v.commit()


if __name__ == "__main__":
    usagetext = "Usage: %prog [options] <vmod.vcc>"
    oparser = optparse.OptionParser(usage=usagetext)

    oparser.add_option('-b', '--boilerplate', action='store_true',
                       default=False,
                       help="Be strict when parsing the input file")
    oparser.add_option('-N', '--strict', action='store_true', default=False,
                       help="Be strict when parsing the input file")
    oparser.add_option('-o', '--output', metavar="prefix", default='vcc_if',
                       help='Output file prefix (default: "vcc_if")')
    oparser.add_option('-w', '--rstdir', metavar="directory", default='.',
                       help='Where to save the generated RST files ' +
                            '(default: ".")')
    oparser.add_option('', '--runtests', action='store_true', default=False,
                       dest="runtests", help=optparse.SUPPRESS_HELP)
    (opts, args) = oparser.parse_args()

    if opts.runtests:
        # Pop off --runtests, pass remaining to unittest.
        del sys.argv[1]
        unittest.main()
        exit()

    i_vcc = None
    if len(args) == 1 and os.path.exists(args[0]):
        i_vcc = args[0]
    elif os.path.exists("vmod.vcc"):
        if not i_vcc:
            i_vcc = "vmod.vcc"
    else:
        print("ERROR: No vmod.vcc file supplied or found.", file=sys.stderr)
        oparser.print_help()
        exit(-1)

    runmain(i_vcc, opts.rstdir, opts.output)
