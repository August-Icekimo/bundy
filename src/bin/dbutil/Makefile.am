SUBDIRS = . tests

bin_SCRIPTS = bundy-dbutil
man_MANS = bundy-dbutil.8

nodist_pylogmessage_PYTHON = $(PYTHON_LOGMSGPKG_DIR)/work/dbutil_messages.py
pylogmessagedir = $(pyexecdir)/bundy/log_messages/

EXTRA_DIST = $(man_MANS) bundy-dbutil.xml dbutil_messages.mes

noinst_SCRIPTS = run_dbutil.sh

CLEANFILES = bundy-dbutil bundy-dbutil.pyc
CLEANFILES += $(PYTHON_LOGMSGPKG_DIR)/work/dbutil_messages.py
CLEANFILES += $(PYTHON_LOGMSGPKG_DIR)/work/dbutil_messages.pyc
CLEANFILES += $(PYTHON_LOGMSGPKG_DIR)/work/dbutil_messages.pyo
DISTCLEANFILES = $(man_MANS)

if GENERATE_DOCS

bundy-dbutil.8: bundy-dbutil.xml
	@XSLTPROC@ --novalid --xinclude --nonet -o $@ http://docbook.sourceforge.net/release/xsl/current/manpages/docbook.xsl $(srcdir)/bundy-dbutil.xml

else

$(man_MANS):
	@echo Man generation disabled.  Creating dummy $@.  Configure with --enable-generate-docs to enable it.
	@echo Man generation disabled.  Remove this file, configure with --enable-generate-docs, and rebuild BUNDY > $@

endif

# Define rule to build logging source files from message file
$(PYTHON_LOGMSGPKG_DIR)/work/dbutil_messages.py : dbutil_messages.mes
	$(top_builddir)/src/lib/log/compiler/message \
	-d $(PYTHON_LOGMSGPKG_DIR)/work -p $(srcdir)/dbutil_messages.mes

bundy-dbutil: dbutil.py $(PYTHON_LOGMSGPKG_DIR)/work/dbutil_messages.py
	$(SED) -e "s|@@PYTHONPATH@@|@pyexecdir@|" \
	       -e "s|@@SYSCONFDIR@@|@sysconfdir@|" \
	       -e "s|@@LIBEXECDIR@@|$(pkglibexecdir)|" dbutil.py >$@
	chmod a+x $@

CLEANDIRS = __pycache__

clean-local:
	rm -rf $(CLEANDIRS)
