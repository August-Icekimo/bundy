// Copyright (C) 2010  Internet Systems Consortium, Inc. ("ISC")
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

// $Id$

#ifndef __HASH_TABLE_H
#define __HASH_TABLE_H

#include <boost/shared_ptr.hpp>
#include <boost/thread.hpp>
#include <boost/interprocess/sync/sharable_lock.hpp>
#include <boost/interprocess/sync/scoped_lock.hpp>
#include <boost/interprocess/sync/interprocess_upgradable_mutex.hpp>
#include <list>

#include "config.h"

#include "hash.h"

// Maximum key length if the maximum size of a DNS name
#define MAX_KEY_LENGTH 255

using namespace std;
using namespace boost::interprocess;

namespace isc {
namespace nsas {

/// \brief Hash Table Slot
///
/// Describes the entry for the hash table.  This is non-copyable (because
/// the mutex is non-copyable), but we need to be able to copy it to initialize
/// a vector of hash table slots.  As the copy is only needed for
/// initialization, and as there is no need to copy state when this happens, we
/// cheat: the copy constructor constructs a newly initialized HashTableSlot and
/// does not copy its argument.
template <typename T>
struct HashTableSlot {
    typedef boost::interprocess::interprocess_upgradable_mutex mutex_type;

    mutex_type                          mutex_;     ///< Protection mutex
    std::list<boost::shared_ptr<T> >    list_;      ///< List head

    /// \brief Default Constructor
    HashTableSlot()
    {}

    /// \brief Copy Constructor
    ///
    /// ... which as noted in the class description does not copy.  It is
    /// defined outside the class to allow for use of the UNUSED_PARAM macro.
    HashTableSlot(const HashTableSlot<T>& unused);

    /// ... and a couple of external definitions
    typedef typename std::list<boost::shared_ptr<T> >::iterator  iterator;
};

// (Non)Copy Constructor

template <typename T>
HashTableSlot<T>::HashTableSlot(const HashTableSlot<T>& unused UNUSED_PARAM) :
    mutex_(), list_()
{}


/// \brief Comparison Object Class
///
/// The base class for a comparison object; this object is used to compare
/// an object in the hash table with a key, and indicates whether the two
/// match.  All objects used for comparison in hash tables should be derived
/// from this class.
template <typename T>
class HashTableCompare {
public:

    /// \brief Comparison Function
    ///
    /// Compares an object against a name in the hash table and reports if the
    /// object's name is the same.
    ///
    /// \param object Pointer to the object
    /// \param key Pointer to the name of the object
    /// \param keylen Length of the key
    ///
    /// \return bool true of the name of the object is equal to the name given.
    virtual bool operator()(T* object, const char* key, uint32_t keylen) = 0;
};


/// \brief Hash Table
///
/// This class is an implementation of a hash table in which the zones and
/// nameservers of the Nameserver Address Store are held.
///
/// A special class has been written (rather than use an existing hash table
/// class) to improve concurrency.  Rather than lock the entire hash table when
/// an object is added/removed/looked up, only the entry for a particular hash
/// value is locked.  To do this, each entry in the hash table is a pair of
/// mutex/STL List; the mutex protects that particular list.
///
/// \param T Class of object to be stored in the table.
template <typename T>
class HashTable {
public:

    /// \brief Constructor
    ///
    /// Initialises the hash table.
    ///
    /// \param CmpFn Compare function (or object) used to compare an object with
    /// to get the name to be used as a key in the table.  The object should be
    /// created via a "new" as ownership passes to the hash table.  The hash
    /// table will take the responsibility of deleting it.
    /// \param size Size of the hash table.  For best result, this should be a
    /// prime although that is not checked.  The default value is the size used
    /// in BIND-9 for its address database.
    HashTable(HashTableCompare<T>* cmp, uint32_t size = 1009);

    /// \brief Get Entry
    ///
    /// Returns a shared_ptr object pointing to the table entry
    ///
    /// \param key Name of the object.  The hash of this is calculated and
    /// used to index the table.
    ///
    /// \return Shared pointer to the object.
    virtual boost::shared_ptr<T> get(const char* key, uint32_t keylen);

    /// \brief Remove Entry
    ///
    /// Remove the specified entry.  The shared pointer to the object is
    /// destroyed, so if this is the last pointer, the object itself is also
    /// destroyed.
    ///
    /// \param key Name of the object.  The hash of this is calculated and
    /// used to index the table.
    /// \param keylen Length of the key
    ///
    /// \return true if the object was deleted, false if it was not found.
    virtual bool remove(const char* key, uint32_t keylen);

    /// \brief Add Entry
    ///
    /// Adds the specified entry to the table.  If there is an entry already
    /// there, it is either replaced or the addition fails, depending on the
    /// setting of the "replace" parameter.
    ///
    /// \param object Pointer to the object to be added.  If the addition is
    /// successful, this object will have a shared pointer pointing to it; it
    /// should not be deleted by the caller.
    /// \param key Key to use to calculate the hash.
    /// \patam keylen Length of "key"
    /// \param replace If true, when an object is added and an object with the
    /// same name already exists, the existing object is replaced.  If false,
    // the addition fails and a status is returned.
    /// \return true if the object was successfully added, false otherwise.
    virtual bool add(boost::shared_ptr<T>& object, const char* key,
        uint32_t keylen, bool replace = false);

    /// \brief Returns Size of Hash Table
    ///
    /// \return Size of hash table
    virtual uint32_t tableSize() const {
        return table_.size();
    }

private:
    Hash                        hash_;  ///< Hashing function
    vector<HashTableSlot<T> >   table_; ///< The hash table itself
    boost::shared_ptr<HashTableCompare<T> > compare_;  ///< Compare object
};


// Constructor
template <typename T>
HashTable<T>::HashTable(HashTableCompare<T>* compare, uint32_t size) :
    hash_(size, MAX_KEY_LENGTH), table_(size), compare_(compare)
{}

// Lookup an object in the table
template <typename T>
boost::shared_ptr<T> HashTable<T>::get(const char* key, uint32_t keylen) {

    // Calculate the hash value
    uint32_t index = hash_(key, keylen);

    // Take out a read lock on this hash slot.  The lock is released when this
    // object goes out of scope.
    sharable_lock<typename HashTableSlot<T>::mutex_type> lock(table_[index].mutex_);

    // Locate the object.
    typename HashTableSlot<T>::iterator i;
    for (i = table_[index].list_.begin(); i != table_[index].list_.end(); ++i) {
        if ((*compare_)(i->get(), key, keylen)) {

            // Found it, so return the shared pointer object
            return (*i);
        }
    }

    // Did not find it, return an empty shared pointer object.
    return boost::shared_ptr<T>();
}

// Remove an entry from the hash table
template <typename T>
bool HashTable<T>::remove(const char* key, uint32_t keylen) {

    // Calculate the hash value
    uint32_t index = hash_(key, keylen);

    // Access to the elements of this hash slot are accessed under a mutex.
    // The mutex will be released when this object goes out of scope and is
    // destroyed.
    scoped_lock<typename HashTableSlot<T>::mutex_type> lock(table_[index].mutex_);

    // Now search this list to see if the element already exists.
    typename HashTableSlot<T>::iterator i;
    for (i = table_[index].list_.begin(); i != table_[index].list_.end(); ++i) {
        if ((*compare_)(i->get(), key, keylen)) {

            // Object found so delete it.
            table_[index].list_.erase(i);
            return true;;
        }
    }

    // When we get here, we know that there is no element with the key in the
    // list, so tell the caller.
    return false;
}

// Add an entry to the hash table
template <typename T>
bool HashTable<T>::add(boost::shared_ptr<T>& object, const char* key,
    uint32_t keylen, bool replace) {

    // Calculate the hash value
    uint32_t index = hash_(key, keylen);

    // Access to the elements of this hash slot are accessed under a mutex.
    scoped_lock<typename HashTableSlot<T>::mutex_type> lock(table_[index].mutex_);

    // Now search this list to see if the element already exists.
    typename HashTableSlot<T>::iterator i;
    for (i = table_[index].list_.begin(); i != table_[index].list_.end(); ++i) {
        if ((*compare_)(i->get(), key, keylen)) {

            // Object found.  If we are not allowed to replace the element,
            // return an error.  Otherwise erase it from the list and exit the
            // loop.
            if (replace) {
                table_[index].list_.erase(i);
                break;
            }
            else {
                return false;
            }
        }
    }

    // When we get here, we know that there is no element with the key in the
    // list - in which case, add the new object.
    table_[index].list_.push_back(object);

    return true;
}

}   // namespace nsas
}   // namespace isc

#endif // __HASH_TABLE_H
