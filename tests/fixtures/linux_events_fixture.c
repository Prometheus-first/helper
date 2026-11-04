#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
struct Buffer {uint64_t reserved;void *text;};
__attribute__((naked,noinline,visibility("default"))) uintptr_t fixture_hook_target(void*a,void*b,void*c){
 __asm__ volatile("push %rbp\n push %r15\n push %r14\n push %r13\n push %r12\n push %rbx\n sub $0x48,%rsp\n mov $7,%eax\n add $0x48,%rsp\n pop %rbx\n pop %r12\n pop %r13\n pop %r14\n pop %r15\n pop %rbp\n ret\n");
}
static void make(unsigned char out[24],const char*s){size_t n=strlen(s);memset(out,0,24);if(n<=22){out[0]=(unsigned char)(n*2);memcpy(out+1,s,n);}else{out[0]=1;uint64_t q=n;memcpy(out+8,&q,8);memcpy(out+16,&s,8);}}
static void emit(uint32_t type,const char*body,uint64_t id){
 unsigned char msg[0x70]={0},from[24],to[24],text[24];make(from,"123456789@chatroom");make(to,"wxid_self_fixture");make(text,body);
 struct Buffer a={0,from},b={0,to},c={0,text};void*pa=&a,*pb=&b,*pc=&c;
 memcpy(msg+8,&pa,8);memcpy(msg+20,&type,4);memcpy(msg+24,&pb,8);memcpy(msg+32,&pc,8);memcpy(msg+0x50,&id,8);if(id)msg[0x6d]|=8;
 fixture_hook_target(0,msg,0);
}
int main(void){usleep(300000);
 emit(1,"wxid_friend_fixture:\nfixture text",18446744073709551614ULL);
 emit(47,"wxid_friend_fixture:\n<msg><emoji cdnurl='fixture'/></msg>",77);
 emit(10002,"<sysmsg type='revokemsg'><revokemsg><session>123456789@chatroom</session><newmsgid>18446744073709551614</newmsgid></revokemsg></sysmsg>",0);
 usleep(1200000);return 0;}
