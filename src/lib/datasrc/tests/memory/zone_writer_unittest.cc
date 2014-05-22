// Copyright (C) 2012  Internet Systems Consortium, Inc. ("ISC")
//
// Permission to use, copy, modify, and/or distribute this software for any
// purpose with or without fee is hereby granted, provided that the above
// copyright notice and this permission notice appear in all copies.
//
// THE SOFTWARE IS PROVIDED "AS IS" AND ISC DISCLAIMS ALL WARRANTIES WITH
// REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF MERCHANTABILITY
// AND FITNESS.  IN NO EVENT SHALL ISC BE LIABLE FOR ANY SPECIAL, DIRECT,
// INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES WHATSOEVER RESULTING FROM
// LOSS OF USE, DATA OR PROFITS, WHETHER IN AN ACTION OF CONTRACT, NEGLIGENCE
// OR OTHER TORTIOUS ACTION, ARISING OUT OF OR IN CONNECTION WITH THE USE OR
// PERFORMANCE OF THIS SOFTWARE.

#include <config.h>

#include <datasrc/memory/zone_writer.h>
#include <datasrc/memory/zone_table_segment_local.h>
#include <datasrc/memory/zone_data.h>
#include <datasrc/memory/zone_data_loader.h>
#include <datasrc/memory/loader_creator.h>
#include <datasrc/memory/zone_table.h>
#include <datasrc/exceptions.h>
#include <datasrc/result.h>

#include <util/memory_segment_mapped.h>

#include <cc/data.h>

#include <dns/rrclass.h>
#include <dns/name.h>

#include <datasrc/tests/memory/memory_segment_mock.h>
#include <datasrc/tests/memory/zone_table_segment_mock.h>

#include <gtest/gtest.h>

#include <boost/scoped_ptr.hpp>
#include <boost/bind.hpp>
#include <boost/format.hpp>

#include <string>
#include <stdexcept>
#include <unistd.h>

using bundy::dns::RRClass;
using bundy::dns::Name;
using bundy::datasrc::ZoneLoaderException;
using namespace bundy::datasrc::memory;
using namespace bundy::datasrc::memory::test;

namespace {

class TestException {};

class MockLoader : public ZoneDataLoader {
public:
    MockLoader(bundy::util::MemorySegment& segment,
               bool* load_called, const bool* load_throw,
               const bool* load_loader_throw, const bool* load_null,
               const bool* load_data, const int* throw_on_commit,
               ZoneData* old_data) :
        load_called_(load_called), load_throw_(load_throw),
        load_loader_throw_(load_loader_throw), load_null_(load_null),
        load_data_(load_data), throw_on_commit_(throw_on_commit),
        num_committed_(0), segment_(segment), old_data_(old_data),
        loaded_data_(NULL), incremental_called_(false)
    {}
    virtual ~MockLoader() {}
    virtual bool isDataReused() const {
        if (*load_null_) {
            return (false);
        }
        if (old_data_) { // if non-NULL, it means we'll reuse it for test
            return (true);
        }
        return (false);
    }
    virtual ZoneData* getLoadedData() const {
        return (loaded_data_);
    }
    virtual ZoneData* load() {
        // We got called
        *load_called_ = true;
        if (*load_throw_) {
            throw TestException();
        }
        if (*load_loader_throw_) {
            bundy_throw(ZoneLoaderException, "faked loader exception");
        }

        if (*load_null_) {
            // Be nasty to the caller and return NULL, which is forbidden
            return (NULL);
        }
        if (old_data_) {
            loaded_data_ = old_data_;
            return (old_data_);
        }
        ZoneData* data = ZoneData::create(segment_, Name("example.org"));
        if (*load_data_) {
            // Put something inside. The node itself should be enough for
            // the tests.
            ZoneNode* node(NULL);
            data->insertName(segment_, Name("subdomain.example.org"), &node);
            EXPECT_NE(static_cast<ZoneNode*>(NULL), node);
        }
        loaded_data_ = data;
        return (data);
    }
    virtual bool loadIncremental(size_t count_limit) {
        // If non-0 count_limit is specified, this mock version always returns
        // false on first call and return true on the second.
        if (count_limit == 0 || incremental_called_) {
            load();
            return (true);
        }
        incremental_called_ = true;
        return (false);
    }
    virtual ZoneData* commit(ZoneData* update_data) {
        // If so specified, throw MemorySegmentGrown once.
        if (*throw_on_commit_ == 1 && num_committed_++ < 1) {
            bundy_throw(bundy::util::MemorySegmentGrown, "test grown");
        } else if (*throw_on_commit_ == 2) {
            bundy_throw(bundy::Unexpected, "test unexpected");
        } else if (*throw_on_commit_ == 3) {
            throw std::runtime_error("test unexpected");
        } else if (*throw_on_commit_ == 4) {
            throw 42;           // not even type of std::exception
        }
        // should be called at most twice, also for preventing infinite loop.
        EXPECT_GE(2, num_committed_);
        return (update_data);
    }

    bool* const load_called_;
    const bool* const load_throw_;
    const bool* const load_loader_throw_;
    const bool* const load_null_;
    const bool* const load_data_;
    const int* const throw_on_commit_;
    unsigned int num_committed_;
private:
    bundy::util::MemorySegment& segment_;
    ZoneData* const old_data_;
    ZoneData* loaded_data_;
    bool incremental_called_;
};

class ZoneWriterTest : public ::testing::Test {
protected:
    ZoneWriterTest() :
        load_called_(false),
        load_throw_(false),
        load_loader_throw_(false),
        load_null_(false),
        load_data_(false),
        throw_on_commit_(0),
        reuse_old_data_(false),
        zt_segment_(new ZoneTableSegmentMock(RRClass::IN(), mem_sgmt_)),
        writer_(new
            ZoneWriter(*zt_segment_,
                       boost::bind(&ZoneWriterTest::loaderCreator,
                                   this, _1, _2),
                       Name("example.org"), RRClass::IN(), false))
    {}
    virtual void TearDown() {
        // Release the writer
        writer_.reset();
    }
    bool load_called_;
    bool load_throw_;
    bool load_loader_throw_;
    bool load_null_;
    bool load_data_;
    // whether to throw from ZoneDataLoader::commit(). 0=none,
    // 1=MemorySegmentGrown, 2=Unexpected, 3=other std exception,
    // 4=even not std derived exception
    int throw_on_commit_;
    bool reuse_old_data_;
    MemorySegmentMock mem_sgmt_;
    boost::scoped_ptr<ZoneTableSegmentMock> zt_segment_;
    boost::scoped_ptr<ZoneWriter> writer_;
public:
    ZoneDataLoader* loaderCreator(bundy::util::MemorySegment& mem_sgmt,
                                  ZoneData* old_data)
    {
        // Make sure it is the correct segment passed. We know the
        // exact instance, can compare pointers to them.
        EXPECT_EQ(&zt_segment_->getMemorySegment(), &mem_sgmt);

        if (!reuse_old_data_) {
            old_data = NULL;
        }
        return (new MockLoader(mem_sgmt_, &load_called_, &load_throw_,
                               &load_loader_throw_, &load_null_,
                               &load_data_, &throw_on_commit_, old_data));
    }
protected:
    void reloadCommon(bool grow_on_commit, size_t count_limit);
    void commitFailCommon(int exception_type);
};

class ReadOnlySegment : public ZoneTableSegmentMock {
public:
    ReadOnlySegment(const bundy::dns::RRClass& rrclass,
                    bundy::util::MemorySegment& mem_sgmt) :
        ZoneTableSegmentMock(rrclass, mem_sgmt)
    {}

    // Returns false indicating that the segment is not usable. We
    // override this too as ZoneTableSegment implementations may use it
    // internally.
    virtual bool isUsable() const {
        return (false);
    }

    // Returns false indicating it is a read-only segment. It is used in
    // the ZoneWriter tests.
    virtual bool isWritable() const {
        return (false);
    }
};

TEST_F(ZoneWriterTest, constructForReadOnlySegment) {
    MemorySegmentMock mem_sgmt;
    ReadOnlySegment ztable_segment(RRClass::IN(), mem_sgmt);
    EXPECT_THROW(ZoneWriter(ztable_segment,
                            boost::bind(&ZoneWriterTest::loaderCreator,
                                        this, _1, _2),
                            Name("example.org"), RRClass::IN(), false),
                 bundy::InvalidOperation);
}

// We call it the way we are supposed to, check every callback is called in the
// right moment.
TEST_F(ZoneWriterTest, correctCall) {
    // Nothing called before we call it
    EXPECT_FALSE(load_called_);

    // Just the load gets called now
    EXPECT_NO_THROW(writer_->load());
    EXPECT_TRUE(load_called_);
    load_called_ = false;

    EXPECT_NO_THROW(writer_->install());
    EXPECT_FALSE(load_called_);

    // We don't check explicitly how this works, but call it to free memory. If
    // everything is freed should be checked inside the TearDown.
    EXPECT_NO_THROW(writer_->cleanup());
}

void
ZoneWriterTest::reloadCommon(bool grow_on_commit, size_t count_limit) {
    const Name zname("example.org");
    reuse_old_data_ = true;

    // First load.  New data should be created.
    if (count_limit > 0) {      // mocked zone data loader requires 2 attempts
        EXPECT_FALSE(writer_->load(count_limit));
        EXPECT_TRUE(writer_->load(count_limit));
    } else {
        writer_->load();
    }
    writer_->install();
    writer_->cleanup();
    const ZoneData* const zd1 =
        zt_segment_->getHeader().getTable()->findZone(zname).zone_data;
    EXPECT_TRUE(zd1);

    // Second load with a new writer.  If so specified, let ZoneData::commit
    // throw the exception.
    if (grow_on_commit) {
        throw_on_commit_ = 1;
    }
    writer_.reset(new ZoneWriter(*zt_segment_,
                                 boost::bind(&ZoneWriterTest::loaderCreator,
                                             this, _1, _2),
                                 zname, RRClass::IN(), false));
    if (count_limit > 0) {
        EXPECT_FALSE(writer_->load(count_limit));
        EXPECT_TRUE(writer_->load(count_limit));
    } else {
        writer_->load();
    }
    writer_->install();
    writer_->cleanup();

    // The same data should still be used (we didn't modify it, so the
    // pointers should be same)
    const ZoneData* const zd2 =
        zt_segment_->getHeader().getTable()->findZone(zname).zone_data;
    EXPECT_EQ(zd1, zd2);
}

TEST_F(ZoneWriterTest, reloadOverridden) {
    reloadCommon(false, 0);
}

TEST_F(ZoneWriterTest, growOnCommit) {
    reloadCommon(true, 0);
}

TEST_F(ZoneWriterTest, reloadOverriddenIncremental) {
    reloadCommon(false, 1000);
}

TEST_F(ZoneWriterTest, growOnCommitIncremental) {
    reloadCommon(true, 10000);
}

// Common test logic for the case where ZoneDataLoader::commit() throws
// unexpected exceptions.  See thrown_on_commit_ above for exception_type.
void
ZoneWriterTest::commitFailCommon(int exception_type) {

    const Name zname("example.org");
    reuse_old_data_ = true;
    
    // First load.  New data should be created.
    writer_->load();
    writer_->install();
    writer_->cleanup();

    // Second load with a new writer.  If so specified, let ZoneData::commit
    // throw the exception.
    throw_on_commit_ = exception_type;
    writer_.reset(new ZoneWriter(*zt_segment_,
                                 boost::bind(&ZoneWriterTest::loaderCreator,
                                             this, _1, _2),
                                 zname, RRClass::IN(), false));
    writer_->load();
    try {
        writer_->install();
        EXPECT_EQ(2, exception_type); // we should reach here only in this case
    } catch (const std::exception&) {
        EXPECT_EQ(3, exception_type);
    } catch (...) {
        EXPECT_EQ(4, exception_type);
    }
    writer_->cleanup();

    // The zone data have become broken, so it's replaced with empty data.
    const ZoneTable::FindResult result =
        static_cast<const ZoneTableSegment&>(*zt_segment_).getHeader().
        getTable()->findZone(zname);
    EXPECT_EQ(bundy::datasrc::result::SUCCESS, result.code);
    EXPECT_TRUE((result.flags & bundy::datasrc::result::ZONE_EMPTY) != 0);
}

TEST_F(ZoneWriterTest, exceptionOnCommit) {
    commitFailCommon(2);
}

TEST_F(ZoneWriterTest, stdExceptionOnCommit) {
    commitFailCommon(3);
}

TEST_F(ZoneWriterTest, intExceptionOnCommit) {
    commitFailCommon(4);
}

TEST_F(ZoneWriterTest, loadTwice) {
    // Load it the first time
    EXPECT_NO_THROW(writer_->load());
    EXPECT_TRUE(load_called_);
    load_called_ = false;

    // The second time, it should not be possible
    EXPECT_THROW(writer_->load(), bundy::InvalidOperation);
    EXPECT_FALSE(load_called_);

    // The object should not be damaged, try installing and clearing now
    EXPECT_NO_THROW(writer_->install());
    EXPECT_FALSE(load_called_);

    // We don't check explicitly how this works, but call it to free memory. If
    // everything is freed should be checked inside the TearDown.
    EXPECT_NO_THROW(writer_->cleanup());
}

// Try loading after call to install and call to cleanup. Both is
// forbidden.
TEST_F(ZoneWriterTest, loadLater) {
    // Load first, so we can install
    EXPECT_NO_THROW(writer_->load());
    EXPECT_NO_THROW(writer_->install());
    // Reset so we see nothing is called now
    load_called_ = false;

    EXPECT_THROW(writer_->load(), bundy::InvalidOperation);
    EXPECT_FALSE(load_called_);

    // Cleanup and try loading again. Still shouldn't work.
    EXPECT_NO_THROW(writer_->cleanup());

    EXPECT_THROW(writer_->load(), bundy::InvalidOperation);
    EXPECT_FALSE(load_called_);
}

// Try calling install at various bad times
TEST_F(ZoneWriterTest, invalidInstall) {
    // Nothing loaded yet
    EXPECT_THROW(writer_->install(), bundy::InvalidOperation);
    EXPECT_FALSE(load_called_);

    EXPECT_NO_THROW(writer_->load());
    load_called_ = false;
    // This install is OK
    EXPECT_NO_THROW(writer_->install());
    // But we can't call it second time now
    EXPECT_THROW(writer_->install(), bundy::InvalidOperation);
    EXPECT_FALSE(load_called_);
}

// We check we can clean without installing first and nothing bad
// happens. We also misuse the testcase to check we can't install
// after cleanup.
TEST_F(ZoneWriterTest, cleanWithoutInstall) {
    EXPECT_NO_THROW(writer_->load());
    EXPECT_NO_THROW(writer_->cleanup());

    EXPECT_TRUE(load_called_);

    // We cleaned up, no way to install now
    EXPECT_THROW(writer_->install(), bundy::InvalidOperation);
}

// Test the case when load callback throws
TEST_F(ZoneWriterTest, loadThrows) {
    load_throw_ = true;
    EXPECT_THROW(writer_->load(), TestException);

    // We can't install now
    EXPECT_THROW(writer_->install(), bundy::InvalidOperation);
    EXPECT_TRUE(load_called_);

    // But we can cleanup
    EXPECT_NO_THROW(writer_->cleanup());
}

// Emulate the situation where load() throws loader error.
TEST_F(ZoneWriterTest, loadLoaderException) {
    std::string error_msg;

    // By default, the exception is propagated.
    load_loader_throw_ = true;
    EXPECT_THROW(writer_->load(), ZoneLoaderException);
    // In this case, passed error_msg won't be updated.
    writer_.reset(new ZoneWriter(*zt_segment_,
                                 boost::bind(&ZoneWriterTest::loaderCreator,
                                             this, _1, _2),
                                 Name("example.org"), RRClass::IN(), false));
    EXPECT_THROW(writer_->load(0, &error_msg), ZoneLoaderException);
    EXPECT_EQ("", error_msg);

    // If we specify allowing load error, load() will succeed and install()
    // adds an empty zone.  Note that we implicitly pass NULL to load()
    // as it's the default parameter, so the following also confirms it doesn't
    // cause disruption.
    writer_.reset(new ZoneWriter(*zt_segment_,
                                 boost::bind(&ZoneWriterTest::loaderCreator,
                                             this, _1, _2),
                                 Name("example.org"), RRClass::IN(), true));
    writer_->load();
    writer_->install();
    writer_->cleanup();

    // Check an empty zone has been really installed.
    using namespace bundy::datasrc::result;
    const ZoneTable* ztable = zt_segment_->getHeader().getTable();
    ASSERT_TRUE(ztable);
    const ZoneTable::FindResult result = ztable->findZone(Name("example.org"));
    EXPECT_EQ(SUCCESS, result.code);
    EXPECT_EQ(ZONE_EMPTY, result.flags);

    // Allowing an error, and passing a template for the error message.
    // It will be filled with the reason for the error.
    writer_.reset(new ZoneWriter(*zt_segment_,
                                 boost::bind(&ZoneWriterTest::loaderCreator,
                                             this, _1, _2),
                                 Name("example.org"), RRClass::IN(), true));
    writer_->load(0, &error_msg);
    EXPECT_NE("", error_msg);

    // In case of no error, the placeholder will be intact.
    load_loader_throw_ = false;
    error_msg.clear();
    writer_.reset(new ZoneWriter(*zt_segment_,
                                 boost::bind(&ZoneWriterTest::loaderCreator,
                                             this, _1, _2),
                                 Name("example.org"), RRClass::IN(), true));
    writer_->load(0, &error_msg);
    EXPECT_EQ("", error_msg);
}

// Check the strong exception guarantee - if it throws, nothing happened
// to the content.
TEST_F(ZoneWriterTest, retry) {
    // First attempt fails due to some exception.
    load_throw_ = true;
    EXPECT_THROW(writer_->load(), TestException);
    // This one shall succeed.
    load_called_ = load_throw_ = false;
    // We want some data inside.
    load_data_ = true;
    EXPECT_NO_THROW(writer_->load());
    // And this one will fail again. But the old data will survive.
    load_data_ = false;
    EXPECT_THROW(writer_->load(), bundy::InvalidOperation);

    // The rest still works correctly
    EXPECT_NO_THROW(writer_->install());
    const ZoneTable* const table(zt_segment_->getHeader().getTable());
    const ZoneTable::FindResult found(table->findZone(Name("example.org")));
    ASSERT_EQ(bundy::datasrc::result::SUCCESS, found.code);
    // For some reason it doesn't seem to work by the ZoneNode typedef, using
    // the full definition instead. At least on some compilers.
    const bundy::datasrc::memory::DomainTreeNode<RdataSet>* node;
    EXPECT_EQ(bundy::datasrc::memory::DomainTree<RdataSet>::EXACTMATCH,
              found.zone_data->getZoneTree().
              find(Name("subdomain.example.org"), &node));
    EXPECT_NO_THROW(writer_->cleanup());
}

// Check the writer defends itsefl when load action returns NULL
TEST_F(ZoneWriterTest, loadNull) {
    load_null_ = true;
    EXPECT_THROW(writer_->load(), bundy::InvalidOperation);

    // We can't install that
    EXPECT_THROW(writer_->install(), bundy::InvalidOperation);

    // It should be possible to clean up safely
    EXPECT_NO_THROW(writer_->cleanup());
}

// Check the object cleans up in case we forget it.
TEST_F(ZoneWriterTest, autoCleanUp) {
    // Load data and forget about it. It should get released
    // when the writer itself is destroyed.
    EXPECT_NO_THROW(writer_->load());
}

// Used in the manyWrites test, encapsulating ZoneDataLoader ctor to avoid
// its signature ambiguity.
ZoneDataLoader*
createLoaderWrapper(bundy::util::MemorySegment& segment, const RRClass& rrclass,
                    const Name& name, const std::string& filename)
{
    return (new ZoneDataLoader(segment, rrclass, name, filename, NULL));
}

// Check the behavior of creating many small zones.  The main purpose of
// test is to trigger MemorySegmentGrown exception in ZoneWriter::install.
// There's no easy (if any) way to cause that reliably as it's highly
// dependent on details of the underlying boost implementation and probably
// also on the system behavior, but we'll try some promising scenario (it
// in fact triggered the intended result at least on one environment).
TEST_F(ZoneWriterTest, manyWrites) {
#ifdef USE_SHARED_MEMORY
    // First, make a fresh mapped file of a small size (so it'll be more likely
    // to grow in the test.
    const char* const mapped_file = TEST_DATA_BUILDDIR "/test.mapped";
    unlink(mapped_file);
    boost::scoped_ptr<bundy::util::MemorySegmentMapped> segment(
        new bundy::util::MemorySegmentMapped(
            mapped_file, bundy::util::MemorySegmentMapped::CREATE_ONLY, 4096));
    segment.reset();

    // Then prepare a ZoneTableSegment of the 'mapped' type specifying the
    // file we just created.
    boost::scoped_ptr<ZoneTableSegment> zt_segment(
        ZoneTableSegment::create(RRClass::IN(), "mapped"));
    const bundy::data::ConstElementPtr params =
        bundy::data::Element::fromJSON(
            "{\"mapped-file\": \"" + std::string(mapped_file) + "\"}");
    zt_segment->reset(ZoneTableSegment::READ_WRITE, params);
#else
    // Do the same test for the local segment, although there shouldn't be
    // anything tricky in that case.
    boost::scoped_ptr<ZoneTableSegment> zt_segment(
        ZoneTableSegment::create(RRClass::IN(), "local"));
#endif

    // Now, create many small zones in the zone table with a ZoneWriter.
    // We use larger origin names so it'll (hopefully) require the memory
    // segment to grow while adding the name into the internal table.
    const size_t zone_count = 10000; // arbitrary choice
    for (size_t i = 0; i < zone_count; ++i) {
        const Name origin(
            boost::str(boost::format("%063u.%063u.%063u.example.org")
                       % i % i % i));
        const ZoneDataLoaderCreator creator =
            boost::bind(createLoaderWrapper, _1, RRClass::IN(), origin,
                        TEST_DATA_DIR "/template.zone");
        ZoneWriter writer(*zt_segment, creator, origin, RRClass::IN(), false);
        writer.load();
        writer.install();
        writer.cleanup();

        // Confirm it's been successfully added and can be actually found.
        const ZoneTable* ztable = zt_segment->getHeader().getTable();
        const ZoneTable::FindResult result = ztable->findZone(origin);
        EXPECT_EQ(bundy::datasrc::result::SUCCESS, result.code);
        EXPECT_NE(static_cast<const ZoneData*>(NULL), result.zone_data) <<
            "unexpected find result: " + origin.toText();
    }

    // Make sure to close the segment before (possibly) removing the mapped
    // file.
    zt_segment.reset();

#ifdef USE_SHARED_MEMORY
    unlink(mapped_file);
#endif
}

}
