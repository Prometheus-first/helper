#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
struct Buffer { uint64_t reserved; void *text; };
struct Msg { unsigned char vtable[8]; void *sender; uint32_t flags; uint32_t type; void *receiver; void *content; unsigned char tail[72]; };
__attribute__((naked,noinline,visibility("default"))) uintptr_t fixture_hook_target(void *a,void *b,void *c) {
 __asm__ volatile("push %rbp\n push %r15\n push %r14\n push %r13\n push %r12\n push %rbx\n sub $0x48,%rsp\n mov $7,%eax\n add $0x48,%rsp\n pop %rbx\n pop %r12\n pop %r13\n pop %r14\n pop %r15\n pop %rbp\n ret\n");
}
static void inline_string(unsigned char out[24],const char *s){size_t n=strlen(s);if(n>22)_exit(10);out[0]=(unsigned char)(n*2);memcpy(out+1,s,n);}
static void heap_string(unsigned char out[24],const char *s,size_t n){out[0]=1;uint64_t len=n;memcpy(out+8,&len,8);memcpy(out+16,&s,8);}
int main(void){
 unsigned char from[24]={0},to[24]={0},body[24]={0};inline_string(from,"123456789@chatroom");inline_string(to,"wxid_self_fixture");
#ifdef HELPER_FIXTURE_MARKER
 const char prefix[]="wxid_friend_fixture:\nWX_LINUX_BRIDGE_08";
#else
 const char prefix[]="wxid_friend_fixture:\n";
#endif
 const char suffix[]="_FIXTURE_END";size_t total=sizeof(prefix)-1+200000+sizeof(suffix)-1;char *text=malloc(total);if(!text)return 11;
 memcpy(text,prefix,sizeof(prefix)-1);memset(text+sizeof(prefix)-1,'x',200000);memcpy(text+total-(sizeof(suffix)-1),suffix,sizeof(suffix)-1);heap_string(body,text,total);
 struct Buffer a={0,from},b={0,to},c={0,body};struct Msg msg={.sender=&a,.type=1,.receiver=&b,.content=&c};
#ifdef HELPER_FIXTURE_SOURCE
 const char source_text[]="<msgsource><atuserlist>wxid_self_fixture</atuserlist></msgsource>";
 unsigned char source[24]={0};heap_string(source,source_text,sizeof(source_text)-1);
 void *source_ptr=source;memcpy((char*)&msg+0x38,&source_ptr,sizeof(source_ptr));
#endif
 usleep(300000);if(fixture_hook_target(NULL,&msg,NULL)!=7)return 12;
#ifdef HELPER_FIXTURE_TWO
 usleep(600000);if(fixture_hook_target(NULL,&msg,NULL)!=7)return 13;
#endif
 usleep(1200000);free(text);puts("fixture_ok");return 0;
}
