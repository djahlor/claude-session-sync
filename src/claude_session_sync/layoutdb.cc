#include <fstream>
#include <iostream>
#include <string>

#include "leveldb/db.h"
#include "leveldb/value_sink.h"
#include "leveldb/write_batch.h"

namespace {

int HexValue(char character) {
  if (character >= '0' && character <= '9') return character - '0';
  if (character >= 'a' && character <= 'f') return character - 'a' + 10;
  if (character >= 'A' && character <= 'F') return character - 'A' + 10;
  return -1;
}

bool DecodeHex(const std::string& encoded, std::string* decoded) {
  if (encoded.size() % 2 != 0) return false;
  decoded->clear();
  decoded->reserve(encoded.size() / 2);
  for (std::string::size_type index = 0; index < encoded.size(); index += 2) {
    const int high = HexValue(encoded[index]);
    const int low = HexValue(encoded[index + 1]);
    if (high < 0 || low < 0) return false;
    decoded->push_back(static_cast<char>((high << 4) | low));
  }
  return true;
}

std::string EncodeHex(const std::string& decoded) {
  static const char digits[] = "0123456789abcdef";
  std::string encoded;
  encoded.reserve(decoded.size() * 2);
  for (std::string::size_type index = 0; index < decoded.size(); ++index) {
    const unsigned char value = static_cast<unsigned char>(decoded[index]);
    encoded.push_back(digits[value >> 4]);
    encoded.push_back(digits[value & 0x0f]);
  }
  return encoded;
}

leveldb::DB* OpenDatabase(const std::string& path) {
  leveldb::Options options;
  options.create_if_missing = false;
  options.paranoid_checks = true;
  leveldb::DB* database = NULL;
  const leveldb::Status status = leveldb::DB::Open(options, path, &database);
  if (!status.ok()) {
    std::cerr << status.ToString() << std::endl;
    return NULL;
  }
  return database;
}

int Get(const std::string& path, const std::string& key_hex) {
  std::string key;
  if (!DecodeHex(key_hex, &key)) {
    std::cerr << "invalid key hex" << std::endl;
    return 2;
  }
  leveldb::DB* database = OpenDatabase(path);
  if (database == NULL) return 1;
  std::string value;
  leveldb::StringValueSink sink(&value);
  leveldb::ReadOptions options;
  options.verify_checksums = true;
  const leveldb::Status status = database->Get(options, key, &sink);
  delete database;
  if (status.IsNotFound()) return 3;
  if (!status.ok()) {
    std::cerr << status.ToString() << std::endl;
    return 1;
  }
  std::cout << EncodeHex(value) << std::endl;
  return 0;
}

int Batch(const std::string& path, const std::string& operations_path) {
  std::ifstream input(operations_path.c_str());
  if (!input) {
    std::cerr << "cannot open operations file" << std::endl;
    return 2;
  }
  leveldb::WriteBatch batch;
  std::string line;
  int operation_count = 0;
  while (std::getline(input, line)) {
    if (line.empty()) continue;
    const std::string::size_type first = line.find('\t');
    const std::string::size_type second =
        first == std::string::npos ? std::string::npos : line.find('\t', first + 1);
    if (first == std::string::npos) {
      std::cerr << "invalid operation" << std::endl;
      return 2;
    }
    const std::string kind = line.substr(0, first);
    const std::string key_hex =
        second == std::string::npos ? line.substr(first + 1)
                                    : line.substr(first + 1, second - first - 1);
    std::string key;
    if (!DecodeHex(key_hex, &key)) {
      std::cerr << "invalid operation key" << std::endl;
      return 2;
    }
    if (kind == "D" && second == std::string::npos) {
      batch.Delete(key);
    } else if (kind == "P" && second != std::string::npos) {
      std::string value;
      if (!DecodeHex(line.substr(second + 1), &value)) {
        std::cerr << "invalid operation value" << std::endl;
        return 2;
      }
      batch.Put(key, value);
    } else {
      std::cerr << "invalid operation shape" << std::endl;
      return 2;
    }
    ++operation_count;
  }
  if (operation_count == 0) {
    std::cerr << "empty operation batch" << std::endl;
    return 2;
  }
  leveldb::DB* database = OpenDatabase(path);
  if (database == NULL) return 1;
  leveldb::WriteOptions options;
  options.sync = true;
  const leveldb::Status status = database->Write(options, &batch);
  delete database;
  if (!status.ok()) {
    std::cerr << status.ToString() << std::endl;
    return 1;
  }
  std::cout << operation_count << std::endl;
  return 0;
}

}  // namespace

int main(int argc, char** argv) {
  if (argc == 4 && std::string(argv[1]) == "get") {
    return Get(argv[2], argv[3]);
  }
  if (argc == 4 && std::string(argv[1]) == "batch") {
    return Batch(argv[2], argv[3]);
  }
  std::cerr << "usage: layoutdb get DATABASE KEY_HEX | "
               "layoutdb batch DATABASE OPERATIONS_FILE"
            << std::endl;
  return 2;
}
